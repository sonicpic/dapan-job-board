"""Read-only Feishu Bitable mirror.

This module deliberately has no dependency on the public job/event schema.  A
successful run mirrors the Base metadata and raw records into the private
Feishu tables; consumers can build typed mappings or search indexes later.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.getenv("FEISHU_API_BASE_URL", "https://open.feishu.cn").rstrip("/")
BASE_TOKEN_RE = re.compile(r"/base/([A-Za-z0-9]+)")


class FeishuError(RuntimeError):
    pass


def configured():
    return bool(os.getenv("FEISHU_APP_ID", "").strip() and
                os.getenv("FEISHU_APP_SECRET", "").strip() and
                target_config()["base_token"])


def token_from_url(url):
    match = BASE_TOKEN_RE.search(url or "")
    return match.group(1) if match else ""


def target_config():
    url = os.getenv("FEISHU_BASE_URL", "").strip()
    return {
        "base_token": os.getenv("FEISHU_BASE_TOKEN", "").strip() or token_from_url(url),
        "table_id": os.getenv("FEISHU_TABLE_ID", "").strip(),
        "view_id": os.getenv("FEISHU_VIEW_ID", "").strip(),
        "url": url,
    }


def _json_request(path, *, method="GET", payload=None, headers=None, timeout=30,
                  retries=3, token=None):
    url = BASE_URL + path
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request_headers = {"Accept": "application/json", "User-Agent": "dapan-job-board/1.0"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = "Bearer " + token
    request_headers.update(headers or {})
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024)
            result = json.loads(raw.decode("utf-8"))
            if result.get("code", 0) not in (0, None):
                raise FeishuError(f"飞书接口错误 {result.get('code')}: {str(result.get('msg') or '')[:300]}")
            return result
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(1000).decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise FeishuError(f"飞书接口 HTTP {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise FeishuError(f"飞书接口连接失败: {exc}") from exc


def _page(path, *, token, params=None):
    params = dict(params or {})
    page_token = ""
    seen_tokens = set()
    while True:
        if page_token:
            params["page_token"] = page_token
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        result = _json_request(path + (("?" + query) if query else ""), token=token)
        data = result.get("data") or {}
        for item in data.get("items") or []:
            yield item
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token or page_token in seen_tokens:
            raise FeishuError("飞书分页响应缺少或重复 page_token")
        seen_tokens.add(page_token)


def tenant_access_token():
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        raise FeishuError("尚未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    data = _json_request("/open-apis/auth/v3/tenant_access_token/internal", method="POST",
                         payload={"app_id": os.environ["FEISHU_APP_ID"],
                                  "app_secret": os.environ["FEISHU_APP_SECRET"]})
    value = (data.get("tenant_access_token") or (data.get("data") or {}).get("tenant_access_token"))
    if not value:
        raise FeishuError("飞书未返回 tenant_access_token")
    return value


def snapshot():
    """Fetch all tables, fields, views and records in one consistent snapshot."""
    cfg = target_config()
    if not cfg["base_token"]:
        raise FeishuError("尚未配置 FEISHU_BASE_TOKEN（或 FEISHU_BASE_URL）")
    access = tenant_access_token()
    app_path = "/open-apis/bitable/v1/apps/" + urllib.parse.quote(cfg["base_token"], safe="")
    tables = list(_page(app_path + "/tables", token=access, params={"page_size": 100}))
    result_tables = []
    for table in tables:
        table_id = table.get("table_id") or table.get("id")
        if not table_id:
            continue
        table_path = app_path + "/tables/" + urllib.parse.quote(table_id, safe="")
        fields = list(_page(table_path + "/fields", token=access, params={"page_size": 100}))
        views = list(_page(table_path + "/views", token=access, params={"page_size": 100}))
        records = list(_page(table_path + "/records", token=access, params={"page_size": 500}))
        result_tables.append({"table": table, "fields": fields, "views": views, "records": records})
    return {"base_token": cfg["base_token"], "tables": result_tables,
            "target_table_id": cfg["table_id"], "target_view_id": cfg["view_id"]}


def public_config():
    cfg = target_config()
    return {"configured": configured() and bool(cfg["base_token"]),
            "base_url": cfg["url"], "target_table_id": cfg["table_id"],
            "target_view_id": cfg["view_id"]}
