import json
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse, HttpResponseBadRequest
import requests
from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponseServerError
from datetime import datetime, timezone
from django.views.decorators.csrf import csrf_exempt

LS_URL = getattr(settings, "LABEL_STUDIO_URL")            # 例如: "https://app.humansignal.com"
LS_TOKEN = getattr(settings, "LABEL_STUDIO_TOKEN")        # 這是你的 PAT（Personal Access Token）
PROJECT_ID = int(getattr(settings, "PROJECT_ID"))
MY_UID = int(getattr(settings, "MY_UID"))
task_ids = []

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


# 假設你已有：
# LS_URL = "https://app.humansignal.com"
# def make_headers(token): return {"Authorization": f"Token {token}"}
# 可選：VIEW_ID = 423937  # 若有設定就會自動帶上

def get_unlabeled_task(project_id: int, token: str, inner_id: int):

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
        "page_size": 20,
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
            tasks = get_unlabeled_task(project_id=PROJECT_ID, token=access,inner_id=inner_id-1)

            global task_ids
            task_ids = [task["id"] for task in tasks]

            return render(request, "index.html", {
                "tasks": enumerate(tasks, start=int(num_tasks_with_annotations)+1),
                "project_id": PROJECT_ID,
                "annotations":int(num_tasks_with_annotations)+1
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

        for id,data in zip(task_ids,batch):
            ok, err = post_annotation(access, id, data['num'], data['aux'].upper())
            if not ok:
                print(err)
            else:
                print(f"Task {id} 真實標註完成")

        return JsonResponse({"ok": True, "received": len(batch)})

