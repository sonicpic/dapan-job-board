"""Read-only Feishu Bitable mirror.

This module deliberately has no dependency on the public job/event schema.  A
successful run mirrors the Base metadata and raw records into the private
Feishu tables; consumers can build typed mappings or search indexes later.
"""
import json
import base64
import datetime as dt
import gzip
import http.cookiejar
import os
import re
import urllib.error
import urllib.parse
import urllib.request


BASE_TOKEN_RE = re.compile(r"/base/([A-Za-z0-9]+)")


class FeishuError(RuntimeError):
    pass


def configured():
    cfg = target_config()
    return bool(cfg["base_token"] and cfg["url"] and cfg["table_id"] and
                os.getenv("FEISHU_PUBLIC_SYNC", "1").strip().lower() not in ("0", "false", "no"))


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


def _public_request(opener, root, path, params=None, timeout=45):
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = root + path + (("?" + query) if query else "")
    try:
        with opener.open(urllib.request.Request(url, headers={
            "Accept": "application/json", "User-Agent": "Mozilla/5.0",
            "Referer": os.getenv("FEISHU_BASE_URL", ""),
        }), timeout=timeout) as response:
            result = json.loads(response.read(16 * 1024 * 1024).decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise FeishuError(f"飞书公开视图接口失败: {exc}") from exc
    if result.get("code", 0) not in (0, None):
        raise FeishuError(f"飞书公开视图错误 {result.get('code')}: {str(result.get('msg') or '')[:300]}")
    return result


def _decode_public(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(gzip.decompress(base64.b64decode(value)).decode("utf-8"))
    except (ValueError, OSError, EOFError, json.JSONDecodeError):
        raise FeishuError("飞书公开视图返回的数据无法解压解析")


def readable_fields(cells, fields):
    """Resolve field IDs, option IDs and rich text while keeping raw cells intact."""
    definitions = {field.get('field_id'): field for field in fields}
    result = {}
    for field_id, cell in (cells or {}).items():
        definition = definitions.get(field_id) or {}
        name = definition.get('field_name') or definition.get('name') or field_id
        value = cell.get('value') if isinstance(cell, dict) and 'value' in cell else cell
        options = {x.get('id'): x.get('name') for x in
                   ((definition.get('property') or {}).get('options') or [])}
        if isinstance(value, list):
            if value and all(isinstance(x, str) for x in value):
                value = '、'.join(options.get(x) or x for x in value)
            elif value and all(isinstance(x, dict) for x in value):
                value = ''.join(str(x.get('text') or x.get('link') or '') for x in value)
        elif isinstance(value, (int, float)) and definition.get('type') == 5:
            try:
                value = dt.datetime.fromtimestamp(value / 1000, dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError, OSError):
                pass
        result[name] = value
    return result


def snapshot():
    """Read a Base shared for anonymous visitors using Feishu's web read API."""
    cfg = target_config()
    if not configured():
        raise FeishuError("尚未配置可公开访问的 FEISHU_BASE_URL")
    origin = urllib.parse.urlsplit(cfg["url"])
    root = f"{origin.scheme}://{origin.netloc}/space/api/v1/bitable/{urllib.parse.quote(cfg['base_token'], safe='')}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", cfg["url"])]
    try:
        with opener.open(cfg["url"], timeout=45) as response:
            if response.status >= 400:
                raise FeishuError(f"飞书共享页面 HTTP {response.status}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise FeishuError(f"飞书共享页面无法访问: {exc}") from exc
    blocks = []
    # The public clientvars response contains the Base block list and all table metadata.
    first_table = cfg["table_id"]
    first_view = cfg["view_id"]
    client = _public_request(opener, root, "/clientvars", {
        "tableID": first_table, "viewID": first_view, "recordLimit": 200,
        "ondemandLimit": 200, "needBase": "true", "viewLazyLoad": "true",
        "ondemandVer": 2, "openType": 0, "noMissCS": "true", "optimizationFlag": 1,
        "removeFmlExtra": "true",
    })
    table_data = _decode_public((client.get("data") or {}).get("table"))
    base_data = _decode_public((client.get("data") or {}).get("base")) or {}
    table_ids = [x for x in (base_data.get("blocks") or []) if str(x).startswith("tbl")]
    if first_table and first_table not in table_ids:
        table_ids.insert(0, first_table)
    if not table_ids:
        table_ids = [first_table]
    for table_id in table_ids:
        if table_id == first_table:
            meta = table_data
        else:
            other = _public_request(opener, root, "/clientvars", {
                "tableID": table_id, "viewID": "", "recordLimit": 200,
                "ondemandLimit": 200, "needBase": "true", "viewLazyLoad": "true",
                "ondemandVer": 2, "openType": 0, "noMissCS": "true",
                "optimizationFlag": 1, "removeFmlExtra": "true",
            })
            meta = _decode_public((other.get("data") or {}).get("table"))
        if not meta:
            raise FeishuError(f"飞书公开视图未返回数据表 {table_id} 的元数据")
        fields = []
        for field_id, field in (meta.get("fieldMap") or {}).items():
            fields.append({"field_id": field_id, **field})
        view_map = meta.get("viewMap") or {}
        views = list(view_map.values())
        view_id = first_view if table_id == first_table and first_view else (views[0].get("id") if views else "")
        records = []
        total = int((meta.get("meta") or {}).get("recordsNum") or meta.get("tableRecordNum") or 0)
        if total <= 0:
            total = int(meta.get("recordCount") or 0)
        revision = int((meta.get("meta") or {}).get("rev") or 0)
        for offset in range(0, total, 3000):
            chunk = _public_request(opener, root, f"/records", {
                "tableId": table_id, "viewId": view_id, "tableRev": revision,
                "depRev": "{}", "viewLazyLoad": "true", "offset": offset,
                "limit": 3000, "tableID": table_id, "viewID": view_id,
                "removeFmlExtra": "true",
            }, timeout=90)
            data = _decode_public((chunk.get("data") or {}).get("records")) or {}
            for record_id, record in (data.get("recordMap") or {}).items():
                records.append({"record_id": record_id, "fields": readable_fields(record, fields), "raw_fields": record,
                                "created_time": (data.get("recordMeta") or {}).get(record_id, {}).get("createdTime"),
                                "last_modified_time": (data.get("recordMeta") or {}).get(record_id, {}).get("modifiedTime")})
            if not (data.get("recordMap") or {}) and offset == 0 and total:
                raise FeishuError(f"飞书公开视图未返回数据表 {table_id} 的记录")
        if total and len({r['record_id'] for r in records}) != total:
            raise FeishuError(f"飞书记录读取不完整：期望 {total} 条，实际 {len({r['record_id'] for r in records})} 条")
        blocks.append({"table": {"table_id": table_id, "name": (base_data.get("blockInfos") or {}).get(table_id, {}).get("name", table_id)},
                       "fields": fields, "views": views, "records": records})
    return {"base_token": cfg["base_token"], "tables": blocks,
            "target_table_id": cfg["table_id"], "target_view_id": cfg["view_id"]}


def public_config():
    cfg = target_config()
    return {"configured": configured(), "mode": "public_view", "public_sync": configured(),
            "base_url": cfg["url"], "target_table_id": cfg["table_id"],
            "target_view_id": cfg["view_id"]}
