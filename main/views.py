import json
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse, HttpResponseBadRequest
import requests
from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponseServerError
from datetime import datetime, timezone
from django.views.decorators.csrf import csrf_exempt
from concurrent.futures import ThreadPoolExecutor, as_completed
MAX_WORKERS = 8
import time

LS_URL = getattr(settings, "LABEL_STUDIO_URL")            # 例如: "https://app.humansignal.com"
LS_TOKEN = getattr(settings, "LABEL_STUDIO_TOKEN")        # 這是你的 PAT（Personal Access Token）
PROJECT_ID = int(getattr(settings, "PROJECT_ID"))
MY_UID = int(getattr(settings, "MY_UID"))
task_ids = []
total = 50
def get_access_token():

    refresh_url = f"{LS_URL}/api/token/refresh/"
    r = requests.post(refresh_url, json={"refresh": LS_TOKEN}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data["access"]

def make_headers(access_token: str):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def get_view_data(ls_url: str, token: str, view_id: int) -> dict:
    headers = {"Authorization": f"Token {token}"}
    r = requests.get(f"{ls_url}/api/dm/views/{view_id}/", headers=headers, timeout=20)
    r.raise_for_status()
    v = r.json()
    # 不同版本有時把 filters/ordering 放在 data 裡
    data = v.get("data") or {}
    data["id"] = v.get("id", view_id)
    return data


def get_unlabeled_task(project_id: int, token: str, inner_id: int, page):

    headers = make_headers(token)

    query_obj = {
        "filters": {
            "conjunction": "and",
            "items": [
                {
                    "filter": "filter:tasks:inner_id",
                    "operator": "greater",
                    "type": "Number",
                    "value": inner_id
                }
            ]
        },
        "ordering": ["tasks:inner_id"]   # 與 inner_id 比較對齊
    }

    params = {
        "project": project_id,
        "page_size": page,
        "page": 1,
        "fields": "task_only",
        "query": json.dumps(query_obj)
    }

    # 若你在程式其他地方有定義 VIEW_ID，就自動帶上，確保與 UI 視圖一致
    if "VIEW_ID" in globals() and globals().get("VIEW_ID"):
        params["view"] = globals()["VIEW_ID"]

    r = requests.get(f"{LS_URL}/api/tasks/", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("tasks", data) if isinstance(data, dict) else (data or [])

def get_views_id(project_id, access_token):
    headers = make_headers(access_token)
    params = {"project": project_id}  # ← 用 project 當 key
    r = requests.post(f"{LS_URL}/api/dm/actions/",
                      headers=headers,
                      params={"id": "next_task", "project": PROJECT_ID},
                      json={"project": PROJECT_ID},
                      timeout=20)
    r.raise_for_status()

    r_proj = requests.get(f"{LS_URL}/api/projects/{PROJECT_ID}",
                      headers=headers,
                      timeout=20)
    r_proj.raise_for_status()
    print(r_proj.json())
    num_tasks_with_annotations = r_proj.json()["num_tasks_with_annotations"]

    return r.json()["inner_id"],num_tasks_with_annotations


def post_annotation(access, task_id, rating="0", relation="I"):

    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return False, f"task_id 非整數：{task_id!r}"
    if task_id <= 0:
        return False, f"task_id 不可為 0 或負數：{task_id}"

    # rating / relation 正規化
    rating = str(rating).strip()
    relation = str(relation).strip().upper()

    allowed_ratings = {"0", "1", "2", "3", "4"}
    allowed_rel = {"E", "S", "C", "I"}
    # 支援全名（Exact/Substitute/Complement/Irrelevant）
    full2abbr = {"EXACT": "E", "SUBSTITUTE": "S", "COMPLEMENT": "C", "IRRELEVANT": "I"}
    if relation not in allowed_rel:
        relation = full2abbr.get(relation.upper(), relation)
    if rating not in allowed_ratings:
        return False, f"rating 僅允許 {sorted(allowed_ratings)}，收到：{rating}"
    if relation not in allowed_rel:
        return False, f"relation 僅允許 {sorted(allowed_rel)}，收到：{relation}"

    headers = make_headers(access)

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ---- payload：不要放 project / task ----
    payload = {
        "lead_time": 5.0,
        "started_at": now_iso,
        "result": [
            {
                "from_name": "rating",
                "to_name": "query",
                "type": "choices",
                "origin": "manual",
                "value": {"choices": [rating]},
            },
            {
                "from_name": "relation",
                "to_name": "query",
                "type": "choices",
                "origin": "manual",
                "value": {"choices": [relation]},
            },
        ],
    }


    url = f"{LS_URL}/api/tasks/{task_id}/annotations/"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code not in (200, 201):
            return False, f"annotation 失敗 {r.status_code} {r.text}"
        return True, r.json()
    except requests.RequestException as e:
        return False, f"HTTP 錯誤：{e}"

@csrf_exempt
def index(request):
    access = get_access_token()
    if request.method == 'GET':
        try:
            inner_id,num_tasks_with_annotations = (get_views_id(project_id=PROJECT_ID, access_token=access))

            tasks = get_unlabeled_task(project_id=PROJECT_ID, token=access,inner_id=inner_id-1,page = total)

            global task_ids
            task_ids = [task["id"] for task in tasks]

            return render(request, "index.html", {
                "tasks": enumerate(tasks, start=int(num_tasks_with_annotations)+1),
                "project_id": PROJECT_ID,
                "annotations":int(num_tasks_with_annotations)+1,
                "total":total # 這次抽取了幾個
            })

        except requests.HTTPError as e:
            # 回傳 API 失敗細節，方便你除錯
            try:
                detail = e.response.text
            except Exception:
                detail = str(e)
            return HttpResponseServerError(f"Label Studio API error: {e}\n\n{detail}")

        except Exception as e:
            return HttpResponseServerError(f"Server error: {e}")
    if request.method == 'POST':
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Invalid JSON")

        batch = payload.get("batch", [])
        cut_index = None

        for idx, b in enumerate(batch):
            if b['num'] is None or b['aux'] is None or '_' in b['combo']:
                cut_index = idx
                break

        if cut_index is not None:
            batch = batch[:cut_index]
            task_ids = task_ids[:cut_index]

        items = []
        for task_id, data in zip(task_ids, batch):
            items.append(
                {
                    "task": task_id,
                    "rating": data["num"],
                    "relation": data["aux"],
                }
            )


        # 多線程
        results = []

        def _send_one(it):
            return it["task"], post_annotation(
                access,
                it["task"],
                it["rating"],
                it["relation"],
            )

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_map = {executor.submit(_send_one, it): it for it in items}
            for fut in as_completed(future_map):
                it = future_map[fut]
                task_id = it["task"]
                try:
                    task_id2, (ok1, err1) = fut.result()
                except Exception as e:
                    results.append((task_id, False, str(e)))
                else:
                    results.append((task_id2, ok1, err1))

        failed = [r for r in results if not r[1]]
        if failed:
            return JsonResponse({
                "errno": False,
                "mode": "single-parallel",
                "failed": failed,
            })

        return JsonResponse({
            "errno": True,
            "mode": "single-parallel",
            "received": len(batch),
        })



        # for id,data in zip(task_ids,batch):
        #     ok, err = post_annotation(access, id, data['num'], data['aux'].upper())
        #     if not ok:
        #         print(err)
        #         return JsonResponse({"errno": False})
        #     else:
        #         print(f"Task {id} 真實標註完成")




def table(request):
    access = get_access_token()
    inner_id, num_tasks_with_annotations = (get_views_id(project_id=PROJECT_ID, access_token=access))
    inner_id -= 50
    num_tasks_with_annotations -= 50
    tasks = get_unlabeled_task(project_id=PROJECT_ID, token=access, inner_id=inner_id - 1, page=total)

    history_datas = []
    for task in tasks:
        data = task.get("data", {})
        query = data.get("query")
        it_name = data.get("IT_NAME")
        image_url = data.get("image_url")

        # ----- 取 Annotation -----
        ann_raw = task.get("annotations_results")

        # 如果是字串 → 轉成 list
        if isinstance(ann_raw, str):
            ann_list = json.loads(ann_raw)
        else:
            ann_list = ann_raw

        first_group = ann_list[0]

        rating = None
        relation = None

        for ann in first_group:
            if ann.get("from_name") == "rating":
                rating = ann["value"]["choices"][0]
            elif ann.get("from_name") == "relation":
                relation = ann["value"]["choices"][0]

        history_datas.append({
            "inner_id":inner_id,
            "num_tasks_with_annotations":num_tasks_with_annotations,
            "query": query,
            "IT_NAME": it_name,
            "image_url": image_url,
            "rating": rating,
            "relation": relation,
        })
        inner_id += 1
        num_tasks_with_annotations += 1


    return render(request,'table.html', {
        "history_datas": history_datas
    })