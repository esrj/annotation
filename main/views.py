import json
from django.utils import timezone as dj_tz
from datetime import timezone as dt_tz
from django.http import JsonResponse, HttpResponseBadRequest
import requests
from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponseServerError
from datetime import datetime, timezone
from django.views.decorators.csrf import csrf_exempt
from concurrent.futures import ThreadPoolExecutor, as_completed
MAX_WORKERS = 8

LS_URL = getattr(settings, "LABEL_STUDIO_URL")            # 例如: "https://app.humansignal.com"
LS_TOKEN = getattr(settings, "LABEL_STUDIO_TOKEN")        # 這是你的 PAT（Personal Access Token）
PROJECT_ID = int(getattr(settings, "PROJECT_ID"))
MY_UID = int(getattr(settings, "MY_UID"))
total = int(getattr(settings, "TOTAL"))

ALLOWED_REL = {'E', 'S', 'C', 'I'}  # ESCI
FETCH_NUM = 100
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
                "total":total, # 這次抽取了幾個
                "t": total-1  # 這次抽取了幾個
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
    return JsonResponse({'error': 'Only GET/POST allowed'}, status=405)

@csrf_exempt
def table(request):
    access = get_access_token()
    base_inner_id, base_num_anns = get_views_id(project_id=PROJECT_ID, access_token=access)
    def parse_rating_relation(task):

        ann_raw = task.get("annotations_results")
        ann_list = None
        if isinstance(ann_raw, str):
            try:
                ann_list = json.loads(ann_raw)
            except json.JSONDecodeError:
                ann_list = []
        elif isinstance(ann_raw, list):
            ann_list = ann_raw
        else:
            ann_list = []

        rating = None
        relation = None

        # 你原始資料看起來像是 [ [ {...}, {...} ] ] 結構，先拿第一組
        if ann_list and isinstance(ann_list[0], list):
            for ann in ann_list[0]:
                if not isinstance(ann, dict):
                    continue
                if ann.get("from_name") == "rating":
                    # 允許沒有 choices 或空陣列
                    choices = (ann.get("value") or {}).get("choices") or []
                    rating = choices[0] if choices else None
                elif ann.get("from_name") == "relation":
                    choices = (ann.get("value") or {}).get("choices") or []
                    relation = choices[0] if choices else None

        return rating, relation
    def build_history_rows(tasks, start_inner_id: int, start_ann_num: int):

        rows = []
        inner_id = int(start_inner_id)
        ann_num = int(start_ann_num)

        for task in tasks:
            data = task.get("data", {}) or {}
            query = data.get("query")
            it_name = data.get("IT_NAME")
            image_url = data.get("image_url")
            rating, relation = parse_rating_relation(task)
            rows.append({
                "task_id":task["id"],
                "inner_id": inner_id,
                "num_tasks_with_annotations": ann_num + 1,
                "query": query,
                "IT_NAME": it_name,
                "image_url": image_url,
                "rating": rating,
                "relation": relation,
            })
            inner_id += 1
            ann_num += 1

        return rows, inner_id, ann_num

    if request.method == 'GET':
        # 這一頁的起始點（往回抓一頁）
        start_inner_id = base_inner_id - FETCH_NUM
        start_ann_num  = base_num_anns - FETCH_NUM

        tasks = get_unlabeled_task(
            project_id=PROJECT_ID,
            token=access,
            inner_id=start_inner_id - 1,  # 你的原邏輯：用 (起點-1) 當條件抓 FETCH_NUM 筆
            page=FETCH_NUM
        )

        history_datas, end_inner_id, end_ann_num = build_history_rows(
            tasks, start_inner_id, start_ann_num
        )

        return render(request, 'table.html', {
            "history_datas": history_datas,
            "annotations": start_ann_num + 1,
            "inner_id": start_inner_id + 1,
            "t":total-1
        })
    if request.method == 'POST':
        try:
            raw = (request.body or b'').decode('utf-8', errors='ignore').strip()
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Invalid JSON")

        if isinstance(payload, list):
            payload = payload[0] if payload else {}

        try:
            current_annotation_num = int(payload.get('current_annotation_num', 0))
            current_inner_id = int(payload.get('current_inner_id', 0))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid fields: current_annotation_num/current_inner_id")

        start_inner_id = current_inner_id - FETCH_NUM - 1
        start_ann_num  = current_annotation_num - FETCH_NUM -1

        tasks = get_unlabeled_task(
            project_id=PROJECT_ID,
            token=access,
            inner_id=start_inner_id - 1,
            page=FETCH_NUM
        )

        history_datas, end_inner_id, end_ann_num = build_history_rows(
            tasks, start_inner_id, start_ann_num
        )

        # 與前端 renderTable 期待一致
        return JsonResponse({
            "history_datas": history_datas,
            "annotations": start_ann_num + 1,
            "inner_id": start_inner_id + 1,
        })

    return JsonResponse({'error': 'Only GET/POST allowed'}, status=405)

def _iso_utc_now():
    return dj_tz.now().astimezone(dt_tz.utc)\
             .isoformat(timespec='milliseconds')\
             .replace('+00:00', 'Z')

import uuid
def _build_result_blocks(rating: int, relation: str):
    """只回傳 result 兩塊，避免多餘欄位造成拒收"""
    return [
        {
            "value": {"choices": [str(relation)]},
            "id": uuid.uuid4().hex[:10],
            "from_name": "relation",  # ← 必須和你的 Labeling Config 一致
            "to_name": "query",       # ← 必須和你的 Labeling Config 一致（物件標籤 name）
            "type": "choices",
            "origin": "manual"
        },
        {
            "value": {"choices": [str(rating)]},
            "id": uuid.uuid4().hex[:10],
            "from_name": "rating",
            "to_name": "query",
            "type": "choices",
            "origin": "manual"
        }
    ]

def _find_annotation_id(task_id: int, project_id: int, token: str):
    headers = make_headers(token)

    # 1) 查 annotations
    try:
        r = requests.get(
            _ls("annotations/"),                     # ← 有 /api 與尾斜線
            headers=headers,
            params={"taskID": task_id, "project": project_id},
            timeout=30
        )
        if r.ok:
            obj = r.json()
            arr = obj if isinstance(obj, list) else (obj.get("results") or obj.get("data") or [])
            if arr:
                arr_sorted = sorted(arr, key=lambda x: (x.get("updated_at") or x.get("created_at") or "", x.get("id") or 0))
                return arr_sorted[-1].get("id")
    except Exception:
        pass

    # 2) 查 task
    try:
        r2 = requests.get(
            _ls(f"tasks/{task_id}/"),               # ← 有 /api 與尾斜線
            headers=headers,
            params={"project": project_id},
            timeout=30
        )
        if r2.ok:
            t = r2.json()
            anns = t.get("annotations") or []
            if isinstance(anns, list) and anns:
                ann_sorted = sorted(anns, key=lambda x: (x.get("updated_at") or x.get("created_at") or "", x.get("id") or 0))
                return ann_sorted[-1].get("id")
            ids = t.get("annotations_ids") or t.get("annotation_ids") or []
            if isinstance(ids, list) and ids:
                return ids[-1]
    except Exception:
        pass

    return None


def _ls(path: str) -> str:

    if not path:
        path = ""
    path = str(path)

    # 已是完整 URL：原樣返回
    if path.startswith("http://") or path.startswith("https://"):
        return path

    # 去掉開頭斜線
    p = path.lstrip('/')

    # 沒帶 api/ 就自動補
    if not p.startswith("api/"):
        p = "api/" + p

    return f"{LS_URL.rstrip('/')}/{p}"

@csrf_exempt
def edit_task(request):
    method = request.method.upper()
    if method == 'POST' and request.headers.get('X-HTTP-Method-Override','').upper() == 'PATCH':
        method = 'PATCH'
    if method != 'PATCH':
        return JsonResponse({'error':'Only PATCH allowed'}, status=405)

    raw = (request.body or b'').decode('utf-8', errors='ignore').strip()
    if not raw:
        return HttpResponseBadRequest('Empty body')
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return JsonResponse({'error':'Invalid JSON','detail':str(e)}, status=400)

    # 讀參數
    try:
        task_id  = int(payload.get('task_id'))
        inner_id = int(payload.get('inner_id'))
        rating   = int(payload.get('rating'))
        relation = str(payload.get('relation') or '').upper()
    except (TypeError, ValueError):
        return JsonResponse({'error':'Invalid types for task_id/inner_id/rating/relation'}, status=400)

    if rating < 0 or rating > 4:
        return JsonResponse({'error':'rating must be 0..4'}, status=400)
    if relation not in ALLOWED_REL:
        return JsonResponse({'error':"relation must be one of 'E','S','C','I'"}, status=400)

    try:
        token = get_access_token()
    except Exception as e:
        return JsonResponse({'error':'failed to get access token', 'detail':str(e)}, status=500)

    headers = make_headers(token)  # 若沒有 make_headers()，改成你定義的 _ls_headers(token)
    result_blocks = _build_result_blocks(rating, relation)

    try:
        # 先找是否已有 annotation
        ann_id = _find_annotation_id(task_id, PROJECT_ID, token)

        if ann_id:
            body = {
                "lead_time": float(payload.get("lead_time") or 0.0),
                "result": result_blocks,
                "draft_id": 0,
                "parent_prediction": None,
                "parent_annotation": None,
                "started_at": _iso_utc_now(),
            }
            resp = requests.patch(
                _ls(f"annotations/{ann_id}/"),  # ← 有 /api 與尾斜線
                headers=headers,
                params={"taskID": task_id, "project": PROJECT_ID},
                json=body,
                timeout=30
            )
            action = "patch"
        else:
            body = {
                "task": task_id,                                # ← 新建時 body 需要 task
                "project": PROJECT_ID,
                "lead_time": float(payload.get("lead_time") or 0.0),
                "result": result_blocks,
                "draft_id": 0,
                "parent_prediction": None,
                "parent_annotation": None,
                "started_at": _iso_utc_now(),
            }
            resp = requests.post(
                _ls("annotations/"),  # ← 有 /api 與尾斜線
                headers=headers,
                json=body,  # body 內含 "task": task_id
                timeout=30
            )
            action = "create"
    except requests.RequestException as e:
        return JsonResponse({'error': 'request to LS failed', 'detail': str(e)}, status=502)

    # 統一錯誤處理（若 LS 回 HTML，就不要整頁丟回前端）
    if not resp.ok:
        ctype = (resp.headers.get('content-type') or '').lower()
        detail = resp.json() if 'application/json' in ctype else resp.text[:800]
        return JsonResponse(
            {"error": "LS API error",
             "status": resp.status_code,
             "url": resp.url,
             "detail": detail},
            status=resp.status_code
        )

    # ✅ 成功回傳（注意：這段要放在上面 if 之外）
    ctype = (resp.headers.get('content-type') or '').lower()
    out = resp.json() if 'application/json' in ctype else {"raw": resp.text}
    ann_id_final = (out.get("id") if isinstance(out, dict) else None) or ann_id

    return JsonResponse({
        "ok": True,
        "action": action,
        "annotation_id": ann_id_final,
        "task_id": task_id,
        "inner_id": inner_id,
        "rating": rating,
        "relation": relation,
        "ls_response": out,
    }, status=200)

