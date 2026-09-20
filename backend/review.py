import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse


SYSTEM_PROMPT = """你是一名谨慎的求职信息研究员。请只根据本次检索到的公开网页材料，归纳求职者对目标公司的讨论。
评价应聚焦应届生求职相关的工作体验、培养发展、薪酬福利、工作强度、管理沟通、稳定性和招聘体验。
网络讨论可能有样本偏差，也可能过时或无法核实；不要把个别说法写成确定事实，不要识别、评价或披露普通员工个人信息。
给出 0 到 100 的参考分：分数越高，公开讨论呈现的求职推荐程度越高。材料不足时应降低结论强度，避免极端分数。
返回 JSON，字段必须为 score（整数）、summary（中文综合总结）、pros（常见正面反馈字符串数组）、cons（常见顾虑字符串数组）、sources（参考来源数组）。
sources 中每项必须包含 title 和 url；只能逐字复制本次搜索结果或实际打开页面中的真实 URL，禁止猜测、补全或编造链接。优先返回 5 至 10 个有效来源。"""


class ProviderResponseError(RuntimeError):
    """Provider returned a completed, billable response that cannot be published."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage or {}


def _env(name, default=''):
    value = os.getenv(name)
    return (value if value and value.strip() else default).strip()


def config():
    mode = _env('REVIEW_MODE').lower()
    if mode == 'openai-web-search':
        missing = [name for name in ('OPENAI_API_KEY',) if not _env(name)]
        return {
            'mode': mode,
            'name': _env('OPENAI_PROVIDER_NAME', 'OpenAI 兼容联网搜索'),
            'configured': not missing,
            'missing': missing,
            'model': _env('OPENAI_MODEL', 'gpt-5-mini'),
        }
    if mode == 'tavily-openai-compatible':
        missing = [name for name in ('TAVILY_API_KEY', 'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL') if not _env(name)]
        return {
            'mode': mode,
            'name': 'Tavily + OpenAI 兼容模型',
            'configured': not missing,
            'missing': missing,
            'model': _env('LLM_MODEL'),
        }
    if mode == 'bailian-web-search':
        missing = [name for name in ('DASHSCOPE_API_KEY',) if not _env(name)]
        return {
            'mode': mode,
            'name': '阿里云百炼联网搜索',
            'configured': not missing,
            'missing': missing,
            'model': _env('BAILIAN_MODEL', 'qwen3.8-flash'),
        }
    return {
        'mode': mode or 'disabled',
        'name': '尚未配置',
        'configured': False,
        'missing': ['REVIEW_MODE'],
        'model': '',
    }


def _post_json(url, payload, headers=None, timeout=90):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json', 'User-Agent': 'JobBoardReview/1.0', **(headers or {})},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode('utf-8'))
            detail = error_body.get('error', error_body.get('message', ''))
            detail = detail.get('message') if isinstance(detail, dict) else str(detail)
        except Exception:
            detail = ''
        raise RuntimeError(f'外部服务返回 HTTP {exc.code}' + (f'：{detail[:180]}' if detail else '')) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError('连接外部搜索或模型服务失败') from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError('外部服务返回了无法解析的数据') from exc


def _post_sse_json(url, payload, headers=None, timeout=180):
    body = json.dumps({**payload, 'stream': True}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                 'User-Agent': 'JobBoardReview/1.0', **(headers or {})},
        method='POST',
    )
    event_name, last_response = '', None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    event_name = ''
                    continue
                if line.startswith('event:'):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if not data or data == '[DONE]':
                    continue
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event_type = item.get('type') or item.get('event') or event_name
                if event_type == 'response.completed':
                    return item.get('response', item)
                if event_type in ('response.failed', 'response.incomplete'):
                    detail = item.get('response', {}).get('error') or item.get('error') or event_type
                    raise RuntimeError('百炼联网分析未完成：' + str(detail)[:300])
                if isinstance(item.get('response'), dict):
                    last_response = item['response']
        if last_response and last_response.get('output'):
            return last_response
        raise RuntimeError('百炼流式响应未返回完整分析结果')
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode('utf-8'))
            detail = error_body.get('error', error_body.get('message', ''))
            detail = detail.get('message') if isinstance(detail, dict) else str(detail)
        except Exception:
            detail = ''
        raise RuntimeError(f'百炼服务返回 HTTP {exc.code}' + (f'：{detail[:180]}' if detail else '')) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError('连接百炼联网搜索服务超时') from exc


def _json_object(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, flags=re.S)
        if not match:
            raise RuntimeError('模型未返回规定的 JSON 结果')
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError('模型返回的分析结果无法解析') from exc


def _source(url, title='', excerpt=''):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return {
        'url': url[:2000],
        'title': (title or parsed.netloc)[:200],
        'excerpt': (excerpt or '')[:600],
    }


def _dedupe_sources(items):
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _source(item.get('url', ''), item.get('title', ''), item.get('excerpt', ''))
        if normalized and normalized['url'] not in seen:
            seen.add(normalized['url'])
            result.append(normalized)
        if len(result) >= 12:
            break
    return result


def _extract_openai(response):
    text_parts, sources = [], []

    def add_sources(value):
        if isinstance(value, str):
            sources.append({'url': value, 'title': ''})
        elif isinstance(value, list):
            for entry in value:
                add_sources(entry)
        elif isinstance(value, dict):
            url = value.get('url') or value.get('link')
            if url:
                sources.append({
                    'url': url,
                    'title': value.get('title') or value.get('name') or '',
                    'excerpt': value.get('excerpt') or value.get('snippet') or '',
                })
            for key in ('url_citation', 'citation', 'source'):
                nested = value.get(key)
                if nested and nested is not value:
                    add_sources(nested)

    for item in response.get('output', []):
        if item.get('type') == 'message':
            for content in item.get('content', []):
                if content.get('type') in ('output_text', 'text') and content.get('text'):
                    text_parts.append(content['text'])
                add_sources(content.get('annotations', []))
        if item.get('type') in ('web_search_call', 'web_search'):
            action = item.get('action') or {}
            # CLIProxyAPI exposes visited pages as action.url rather than
            # web_search_call.action.sources.  These are the exact pages the
            # search tool opened and are safe to present as references.
            add_sources(action.get('url'))
            add_sources(action.get('sources', []))
            add_sources(action.get('citations', []))
            add_sources(action.get('results', []))
            add_sources(item.get('sources', []))
            add_sources(item.get('citations', []))
    add_sources(response.get('sources', []))
    add_sources(response.get('citations', []))
    if not text_parts and response.get('output_text'):
        text_parts.append(response['output_text'])
    result = _json_object('\n'.join(text_parts))
    if isinstance(result, dict):
        add_sources(result.pop('sources', []))
    return result, _dedupe_sources(sources)


def _response_usage(response):
    usage = response.get('usage') or {}
    return {
        'input_tokens': int(usage.get('input_tokens') or 0),
        'output_tokens': int(usage.get('output_tokens') or 0),
        'total_tokens': int(usage.get('total_tokens') or 0),
        'search_count': sum(
            item.get('type') in ('web_search_call', 'web_search')
            for item in response.get('output', [])
        ),
    }


def _openai_web_search(company):
    base = _env('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['score', 'summary', 'pros', 'cons', 'sources'],
        'properties': {
            'score': {'type': 'integer', 'minimum': 0, 'maximum': 100},
            'summary': {'type': 'string'},
            'pros': {'type': 'array', 'items': {'type': 'string'}},
            'cons': {'type': 'array', 'items': {'type': 'string'}},
            'sources': {
                'type': 'array',
                'minItems': 3,
                'maxItems': 10,
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['title', 'url'],
                    'properties': {
                        'title': {'type': 'string'},
                        'url': {'type': 'string'},
                    },
                },
            },
        },
    }
    payload = {
        'model': _env('OPENAI_MODEL', 'gpt-5-mini'),
        'instructions': SYSTEM_PROMPT,
        'input': (
            f'搜索并分析“{company[:160]}”与求职、工作体验有关的近期公开中文网评。'
            '通过有针对性的搜索核验材料，优先打开五到十个不同来源页面，并尽量覆盖至少三个平台。'
            '避免重复页面和无关结果；只有公开材料确实不足时才可以少于五个来源。'
            '优先选择近三年的信息；公司同名时先核对招聘语境。'
        ),
        'tools': [{'type': _env('OPENAI_WEB_SEARCH_TOOL', 'web_search')}],
        'include': ['web_search_call.action.sources'],
        'text': {'format': {'type': 'json_schema', 'name': 'company_review', 'strict': True, 'schema': schema}},
        'reasoning': {'effort': _env('OPENAI_REASONING_EFFORT', 'max')},
        'max_output_tokens': int(_env('OPENAI_MAX_OUTPUT_TOKENS', '1200')),
    }
    response = _post_sse_json(
        base + '/responses', payload,
        {
            'Authorization': 'Bearer ' + _env('OPENAI_API_KEY'),
            'User-Agent': _env('OPENAI_USER_AGENT', 'Mozilla/5.0 (compatible; JobBoardReview/1.0)'),
        },
    )
    usage = _response_usage(response)
    try:
        result, sources = _extract_openai(response)
    except RuntimeError as exc:
        raise ProviderResponseError(str(exc), usage) from exc
    if not sources:
        raise ProviderResponseError('联网搜索未返回可核验的参考来源', usage)
    return result, sources, usage


def _tavily_search(company):
    payload = {
        'api_key': _env('TAVILY_API_KEY'),
        'query': f'"{company[:160]}" 员工 工作体验 校招 薪资 加班 管理 评价 讨论',
        'topic': 'general',
        'search_depth': 'advanced',
        'max_results': 10,
        'include_answer': False,
        'include_raw_content': False,
    }
    response = _post_json(_env('TAVILY_BASE_URL', 'https://api.tavily.com') .rstrip('/') + '/search', payload, timeout=60)
    return _dedupe_sources([
        {'url': item.get('url', ''), 'title': item.get('title', ''), 'excerpt': item.get('content', '')}
        for item in response.get('results', [])
    ])


def _compatible_analysis(company, sources):
    evidence = '\n\n'.join(
        f'[{index}] {item["title"]}\n{item["excerpt"]}'
        for index, item in enumerate(sources, 1)
    )
    payload = {
        'model': _env('LLM_MODEL'),
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': f'目标公司：{company[:160]}\n\n以下是搜索服务返回的网页标题和摘要：\n{evidence}'},
        ],
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
    }
    response = _post_json(
        _env('LLM_BASE_URL').rstrip('/') + '/chat/completions', payload,
        {'Authorization': 'Bearer ' + _env('LLM_API_KEY')},
    )
    choices = response.get('choices') or []
    if not choices:
        raise RuntimeError('模型没有返回分析结果')
    return _json_object(choices[0].get('message', {}).get('content', ''))


def _bailian_web_search(company):
    payload = {
        'model': _env('BAILIAN_MODEL', 'qwen3.8-flash'),
        'instructions': SYSTEM_PROMPT + '\n必须先使用 web_search 联网检索；如果没有执行搜索，就不要给出分析或评分。',
        'input': (
            f'请联网搜索并分析“{company[:160]}”与求职、工作体验有关的近期公开中文网评。'
            '优先查找多个相互独立的平台和近三年的内容，关注应届生与普通岗位体验；公司同名时先核对招聘语境。'
            '完成两到三轮有针对性的搜索即可，避免无关检索。'
            '务必根据搜索材料输出规定的 JSON，不要在 JSON 外添加文字。'
        ),
        'tools': [{'type': 'web_search'}],
        'enable_thinking': _env('BAILIAN_ENABLE_THINKING', 'false').lower() in ('1', 'true', 'yes'),
        'max_output_tokens': 2500,
    }
    response = _post_sse_json(
        _env('BAILIAN_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1').rstrip('/') + '/responses',
        payload,
        {'Authorization': 'Bearer ' + _env('DASHSCOPE_API_KEY')},
        timeout=180,
    )
    result, sources = _extract_openai(response)
    if not sources:
        raise RuntimeError('百炼联网搜索未返回可核验的参考来源')
    return result, sources


def _validated(result, sources, usage=None):
    try:
        score = int(result.get('score'))
    except (TypeError, ValueError):
        raise RuntimeError('模型返回的推荐分不合法')
    if not 0 <= score <= 100:
        raise RuntimeError('模型返回的推荐分超出范围')
    summary = str(result.get('summary') or '').strip()[:4000]
    if not summary:
        raise RuntimeError('模型没有返回网评总结')
    def points(name):
        value = result.get(name) or []
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()][:6]
    label = '较推荐' if score >= 75 else '可以关注' if score >= 60 else '谨慎参考' if score >= 40 else '风险较多'
    return {
        'score': score,
        'label': label,
        'summary': summary,
        'pros': points('pros'),
        'cons': points('cons'),
        'sources': _dedupe_sources(sources),
        'usage': usage or {},
    }


def analyze(company):
    cfg = config()
    if not cfg['configured']:
        raise RuntimeError('网评分析服务尚未配置完整')
    usage = {}
    if cfg['mode'] == 'openai-web-search':
        result, sources, usage = _openai_web_search(company)
    elif cfg['mode'] == 'tavily-openai-compatible':
        sources = _tavily_search(company)
        if not sources:
            raise RuntimeError('搜索服务没有返回可核验的参考来源')
        result = _compatible_analysis(company, sources)
    elif cfg['mode'] == 'bailian-web-search':
        result, sources = _bailian_web_search(company)
    else:
        raise RuntimeError('不支持的网评分析模式')
    try:
        return _validated(result, sources, usage), cfg
    except RuntimeError as exc:
        if usage:
            raise ProviderResponseError(str(exc), usage) from exc
        raise
