import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


ASR_MODEL = 'qwen-audio-3.0-asr-flash-filetrans'


def _env(name, default=''):
    value = os.getenv(name)
    return (value if value and value.strip() else default).strip()


def config():
    missing = [
        name for name in ('ASR_DASHSCOPE_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL')
        if not _env(name)
    ]
    return {
        'configured': not missing,
        'missing': missing,
        'asr_model': _env('ASR_MODEL', ASR_MODEL),
        'summary_model': _env('OPENAI_MODEL'),
    }


def _json_request(url, payload=None, headers=None, method=None, timeout=120):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        method=method or ('POST' if body is not None else 'GET'),
        headers={
            'Accept': 'application/json',
            'User-Agent': 'JobBoardRecording/1.0',
            **({'Content-Type': 'application/json; charset=utf-8'} if body is not None else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            details = json.loads(raw)
            message = details.get('message') or details.get('error', {}).get('message') or details.get('code')
        except Exception:
            message = ''
        request_id = exc.headers.get('x-request-id') or exc.headers.get('x-dashscope-request-id')
        cf_ray = exc.headers.get('cf-ray')
        extra = []
        if message:
            extra.append(str(message)[:500])
        elif exc.code == 524:
            extra.append('上游网关等待模型响应超时')
        if request_id:
            extra.append('request_id=' + request_id[:120])
        if cf_ray:
            extra.append('cf-ray=' + cf_ray[:120])
        raise RuntimeError(f'外部服务返回 HTTP {exc.code}' + (('：' + '；'.join(extra)) if extra else '')) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError('连接外部音频或总结服务失败') from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError('外部服务返回了无法解析的数据') from exc


def submit_asr(file_url):
    base = _env('ASR_DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1').rstrip('/')
    response = _json_request(
        base + '/services/audio/asr/transcription',
        {
            'model': _env('ASR_MODEL', ASR_MODEL),
            'input': {'file_urls': [file_url]},
            'parameters': {
                'channel_id': [0],
                'diarization_enabled': True,
            },
        },
        {
            'Authorization': 'Bearer ' + _env('ASR_DASHSCOPE_API_KEY'),
            'X-DashScope-Async': 'enable',
        },
    )
    task_id = (response.get('output') or {}).get('task_id')
    if not task_id:
        raise RuntimeError('百炼没有返回转写任务编号')
    return task_id


def asr_audio_path(file_path):
    source = Path(file_path)
    return source.with_name(source.stem + '.asr-mono.m4a')


def prepare_asr_audio(file_path):
    """Create a mono speech copy so DashScope speaker diarization can run."""
    source = Path(file_path)
    target = asr_audio_path(source)
    if target.is_file() and target.stat().st_size and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    temporary = target.with_name(target.stem + '.processing.m4a')
    temporary.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            [
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', str(source), '-vn', '-map_metadata', '-1', '-ac', '1', '-ar', '16000',
                '-c:a', 'aac', '-b:a', '64k', str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        if completed.returncode or not temporary.is_file() or not temporary.stat().st_size:
            detail = (completed.stderr or '').strip().splitlines()
            suffix = ('：' + detail[-1][:200]) if detail else ''
            raise RuntimeError('无法为说话人分离准备单声道录音' + suffix)
        temporary.replace(target)
        target.chmod(0o600)
        return target
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('准备单声道录音超时') from exc
    finally:
        temporary.unlink(missing_ok=True)


def wait_asr(task_id, on_status=None, poll_seconds=8, timeout_seconds=21600):
    base = _env('ASR_DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1').rstrip('/')
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        response = _json_request(
            base + '/tasks/' + task_id,
            headers={'Authorization': 'Bearer ' + _env('ASR_DASHSCOPE_API_KEY')},
            timeout=60,
        )
        output = response.get('output') or {}
        status = str(output.get('task_status') or output.get('status') or '').upper()
        if status != last_status and on_status:
            on_status(status)
        last_status = status
        if status == 'SUCCEEDED':
            return output
        if status in ('FAILED', 'CANCELED', 'UNKNOWN'):
            detail = output.get('message') or output.get('task_metrics') or response.get('message') or status
            raise RuntimeError('百炼转写任务失败：' + str(detail)[:300])
        time.sleep(poll_seconds)
    raise RuntimeError('百炼转写等待超时，可在后台重新启动')


def _result_urls(value):
    urls = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('transcription_url', 'result_url') and isinstance(item, str) and item.startswith(('http://', 'https://')):
                urls.append(item)
            else:
                urls.extend(_result_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_result_urls(item))
    return list(dict.fromkeys(urls))


def _transcript_text(value):
    if isinstance(value, dict):
        transcripts = value.get('transcripts')
        if isinstance(transcripts, list):
            parts = [_transcript_text(item) for item in transcripts]
            return '\n\n'.join(part for part in parts if part)
        sentences = value.get('sentences')
        if isinstance(sentences, list) and any(
            isinstance(item, dict) and item.get('speaker_id') is not None for item in sentences
        ):
            turns = []
            for sentence in sentences:
                if not isinstance(sentence, dict):
                    continue
                text = sentence.get('text')
                speaker = sentence.get('speaker_id')
                if not isinstance(text, str) or not text.strip():
                    continue
                label = f'说话人 {speaker + 1}' if isinstance(speaker, int) else '说话人'
                if turns and turns[-1][0] == label:
                    turns[-1][1].append(text.strip())
                else:
                    turns.append([label, [text.strip()]])
            if turns:
                return '\n\n'.join(f'{label}：{"".join(parts)}' for label, parts in turns)
        text = value.get('text')
        if isinstance(text, str) and text.strip():
            return text.strip()
        if isinstance(sentences, list):
            parts = [_transcript_text(item) for item in sentences]
            return '\n'.join(part for part in parts if part)
        for key in ('transcript', 'content', 'result'):
            if key in value:
                found = _transcript_text(value[key])
                if found:
                    return found
    elif isinstance(value, list):
        parts = [_transcript_text(item) for item in value]
        return '\n\n'.join(part for part in parts if part)
    elif isinstance(value, str) and value.strip():
        return value.strip()
    return ''


def fetch_transcript(output):
    documents = []
    for url in _result_urls(output):
        documents.append(_json_request(url, timeout=120))
    if not documents:
        documents.append(output)
    transcript = '\n\n'.join(filter(None, (_transcript_text(item) for item in documents))).strip()
    if not transcript:
        raise RuntimeError('百炼任务完成，但结果中没有可保存的转写文字')
    return transcript


def _response_text(response):
    parts = []
    for item in response.get('output') or []:
        if item.get('type') != 'message':
            continue
        for content in item.get('content') or []:
            if content.get('type') in ('output_text', 'text') and content.get('text'):
                parts.append(content['text'])
    if not parts and response.get('output_text'):
        parts.append(response['output_text'])
    text = '\n'.join(parts).strip()
    if not text:
        raise RuntimeError('总结模型没有返回文字')
    return text


def _sse_request(url, payload, headers=None, timeout=300):
    body = json.dumps({**payload, 'stream': True}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        method='POST',
        headers={
            'Accept': 'text/event-stream',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'JobBoardRecording/1.0',
            **(headers or {}),
        },
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
                    raise RuntimeError('总结模型流式响应未完成：' + str(detail)[:500])
                if isinstance(item.get('response'), dict):
                    last_response = item['response']
        if last_response and last_response.get('output'):
            return last_response
        raise RuntimeError('总结模型流式响应结束，但没有返回完整结果')
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            details = json.loads(raw)
            message = details.get('message') or details.get('error', {}).get('message') or details.get('code')
        except Exception:
            message = ''
        cf_ray = exc.headers.get('cf-ray')
        extra = [str(message)[:500]] if message else []
        if cf_ray:
            extra.append('cf-ray=' + cf_ray[:120])
        raise RuntimeError(f'外部服务返回 HTTP {exc.code}' + (('：' + '；'.join(extra)) if extra else '')) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError('连接外部总结服务失败') from exc


def _call_summary(instructions, content, max_output_tokens):
    configured_limit = _env('RECORDING_MAX_OUTPUT_TOKENS', _env('OPENAI_MAX_OUTPUT_TOKENS', '2500'))
    try:
        max_output_tokens = min(max_output_tokens, max(600, int(configured_limit)))
    except ValueError:
        max_output_tokens = min(max_output_tokens, 2500)
    response = _sse_request(
        _env('OPENAI_BASE_URL').rstrip('/') + '/responses',
        {
            'model': _env('OPENAI_MODEL'),
            'instructions': instructions,
            'input': content,
            'reasoning': {'effort': _env('RECORDING_REASONING_EFFORT', 'max')},
            'max_output_tokens': max_output_tokens,
        },
        {
            'Authorization': 'Bearer ' + _env('OPENAI_API_KEY'),
            'User-Agent': _env('OPENAI_USER_AGENT', 'Mozilla/5.0 (compatible; JobBoardRecording/1.0)'),
        },
        timeout=240,
    )
    return _response_text(response)


EXTRACT_PROMPT = '''你正在整理一段校园招聘宣讲会录音的转写片段。只提取这段中明确出现的信息，供后续汇总使用。
完整保留有实际意义的事实、数字、日期、时间、地点、岗位、部门、职责、资格、招聘流程、截止时间、待遇福利、培养晋升、工作安排、联系方式和现场问答。
忽略寒暄、口头禅、重复内容、噪声、与宣讲无关的私人对话。不要补全或猜测听不清的内容；遇到语义中断、明显缺失或无法确认之处要标注。使用结构清晰的中文纯文本。'''

FINAL_PROMPT = '''你是一名严谨的校园招聘信息编辑。根据提供的宣讲会转写或分段事实笔记，写一份全面、可核对的中文总结。
不能遗漏录音中出现的重要岗位、部门、职责、任职要求、招聘对象、投递与面试流程、时间节点、地点、薪资福利、培养晋升、工作安排、联系方式以及现场问答。合并重复内容，过滤寒暄、杂音、口头禅和无关私人对话。
只依据输入内容，不得补充常识、推算或猜测。开头提供的企业和宣讲时间只用于标识这场活动，不能用于补全年份、日期或其他录音未明确说明的事实。录音或转写存在开头/结尾缺失、中断、听不清、上下文不足时，必须在“录音完整性说明”中如实指出；无法确认的数字或专有名词也要明确标注。
必须输出标准 Markdown 文档，不要使用 Markdown 代码围栏，不要在正文前后添加解释。使用二级标题“## 核心信息”“## 岗位与要求”“## 招聘流程与时间”“## 待遇与发展”“## 现场问答”“## 录音完整性说明”；适合并列的信息使用无序列表，步骤使用有序列表，关键时间、数字和结论可使用粗体。没有提到的项目写“录音中未明确提及”，不要省略整个重要栏目。'''

INTERVIEW_EXTRACT_PROMPT = '''你正在整理一段求职面试录音的转写片段。只提取录音中明确出现、对复盘有帮助的信息。
按发生顺序保留面试阶段、面试官问题、候选人回答要点、技术题与解题过程、追问、行为面问题、候选人反问、面试官介绍的岗位或团队信息、明确反馈、后续安排和待补充事项。
区分事实、面试官观点和候选人自述，不替任何一方补全答案或推测评价。忽略寒暄、口头禅、重复内容、噪声和无关私人对话；听不清、录音中断或上下文缺失之处要标注。使用结构清晰的中文纯文本。'''

INTERVIEW_FINAL_PROMPT = '''你是一名严谨的求职面试复盘编辑。根据面试录音转写或分段事实笔记，制作一份完整、可核对、便于后续改进的中文面试记录。
必须保留面试流程、每一道有意义的问题、回答要点、技术题思路与结果、追问、行为面交流、候选人反问、岗位与团队信息、面试官明确表达的反馈、后续安排。对回答表现的总结必须以录音证据为依据：明确区分“录音中明确反馈”和“基于回答内容的复盘建议”，不得臆测录用倾向或面试官态度。
过滤寒暄、杂音、口头禅和无关私人对话。专有名词、数字或话语听不清时要明确标注。录音缺少开头、结尾或部分环节时，在完整性说明中如实说明。
必须输出标准 Markdown 文档，不要使用 Markdown 代码围栏，不要在正文前后添加解释。使用二级标题“## 面试概况”“## 流程与时间线”“## 问题与回答”“## 技术题与解题过程”“## 面试官反馈与信号”“## 候选人反问及岗位信息”“## 后续安排”“## 复盘建议”“## 录音完整性说明”。问题较多时使用三级标题或有序列表；关键结论可使用粗体。没有提到的项目写“录音中未明确提及”，不要省略整个重要栏目。'''

INTERVIEW_SECTION_PROMPTS = [
    INTERVIEW_FINAL_PROMPT + '''\n这次只输出以下三个二级标题及其内容：“## 面试概况”“## 流程与时间线”“## 问题与回答”。逐项保留所有有意义的问题、回答要点和追问，不要输出其他章节。''',
    INTERVIEW_FINAL_PROMPT + '''\n这次只输出以下四个二级标题及其内容：“## 技术题与解题过程”“## 面试官反馈与信号”“## 候选人反问及岗位信息”“## 后续安排”。不得把推测写成面试官反馈，不要输出其他章节。''',
    INTERVIEW_FINAL_PROMPT + '''\n这次只输出以下两个二级标题及其内容：“## 复盘建议”“## 录音完整性说明”。复盘建议必须对应录音中的具体回答或表现；如实说明缺失、听不清和中断，不要输出其他章节。''',
]


def _chunks(text, size=12000):
    text = text.strip()
    chunks = []
    while len(text) > size:
        cut = max(text.rfind('\n', 0, size), text.rfind('。', 0, size), text.rfind('？', 0, size))
        if cut < size // 2:
            cut = size
        else:
            cut += 1
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def summarize(transcript, company='', event_time='', kind='event'):
    chunks = _chunks(transcript)
    interview = kind == 'interview'
    extract_prompt = INTERVIEW_EXTRACT_PROMPT if interview else EXTRACT_PROMPT
    final_prompt = INTERVIEW_FINAL_PROMPT if interview else FINAL_PROMPT
    context = (
        f'面试公司或主题：{company or "未注明"}\n面试时间：{event_time or "未注明"}\n\n'
        if interview else
        f'宣讲企业或活动：{company or "未注明"}\n宣讲时间：{event_time or "未注明"}\n\n'
    )
    if interview and len(transcript) > 8000:
        content = context + '完整面试转写：\n' + transcript
        limits = (1300, 1300, 800)
        return '\n\n'.join(
            _call_summary(prompt, content, limit)
            for prompt, limit in zip(INTERVIEW_SECTION_PROMPTS, limits)
        )
    if len(chunks) == 1:
        return _call_summary(final_prompt, context + '完整转写：\n' + chunks[0], 5000)
    notes = []
    for index, chunk in enumerate(chunks, 1):
        notes.append(_call_summary(
            extract_prompt,
            context + f'这是完整录音转写的第 {index}/{len(chunks)} 段：\n' + chunk,
            2500,
        ))
    joined = '\n\n'.join(f'【第 {index} 段事实笔记】\n{note}' for index, note in enumerate(notes, 1))
    return _call_summary(final_prompt, context + '以下是按原始顺序提取的全部分段事实笔记：\n\n' + joined, 5000)
