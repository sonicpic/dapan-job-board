import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from source import SOURCE, TZ, fetch_source, safe_link, normalize_date
import review
import recording

DATA = Path(os.getenv('DATA_DIR', '/app/data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'jobs.sqlite3'
ORIGIN = os.getenv('APP_ORIGIN', 'http://localhost:8000')
ORIGINS = {ORIGIN, *(x.strip() for x in os.getenv('APP_ORIGINS', '').split(',') if x.strip())}
INTERVAL = 900
wake = threading.Event()
stop = threading.Event()
sync_lock = threading.Lock()
review_lock = threading.Lock()
review_wake = threading.Event()
recording_lock = threading.Lock()
recording_wake = threading.Event()
MAX_REVIEW_PENDING = 5
MAX_AUDIO_BYTES = int(os.getenv('MAX_AUDIO_UPLOAD_MB', '500')) * 1024 * 1024
RECORDINGS = DATA / 'recordings'
RECORDINGS.mkdir(parents=True, exist_ok=True)
RECORDINGS.chmod(0o700)
ALLOWED_AUDIO_EXTENSIONS = {'.m4a', '.mp3', '.mp4', '.wav', '.aac', '.flac', '.ogg', '.webm'}
DEFAULTS = {'title': '大潘的就业情报站', 'subtitle': '软件学院 · 校园招聘与宣讲会',
            'announcement': '信息来自学院就业共享表格。岗位要求与时间安排请以企业最新公告为准。',
            'source_url': SOURCE, 'auto_sync': True}


def now():
    return dt.datetime.now(TZ).isoformat(timespec='seconds')


def conn():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def check_pw(password, stored):
    return hmac.compare_digest(hash_pw(password, stored.split(':')[0]), stored)


def secure_request(request):
    forwarded = request.headers.get('x-forwarded-proto')
    return (forwarded.split(',')[0].strip() if forwarded else request.url.scheme) == 'https'


def settings():
    with conn() as c:
        return DEFAULTS | {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}


def put_setting(c, key, value):
    c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))


def init():
    with conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,source TEXT NOT NULL,data TEXT NOT NULL,present INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS overrides(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deleted_records(id TEXT PRIMARY KEY,source TEXT,source_key TEXT,deleted_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,started TEXT,finished TEXT,status TEXT,jobs INTEGER,events INTEGER,changed INTEGER,message TEXT);
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY,password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,username TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT,at REAL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,action TEXT,record_id TEXT);
        CREATE TABLE IF NOT EXISTS company_reviews(
          company TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          score INTEGER,
          label TEXT,
          summary TEXT,
          pros TEXT NOT NULL DEFAULT '[]',
          cons TEXT NOT NULL DEFAULT '[]',
          sources TEXT NOT NULL DEFAULT '[]',
          provider TEXT,
          usage TEXT NOT NULL DEFAULT '{}',
          updated_at TEXT,
          queued_at TEXT,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS event_recordings(
          record_id TEXT PRIMARY KEY,
          original_name TEXT NOT NULL,
          file_path TEXT NOT NULL,
          mime_type TEXT,
          size INTEGER NOT NULL,
          status TEXT NOT NULL,
          asr_task_id TEXT,
          transcript TEXT,
          summary TEXT,
          summary_public INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          error_stage TEXT,
          error_detail TEXT,
          process_log TEXT NOT NULL DEFAULT '[]',
          source_token_hash TEXT,
          source_token_expires REAL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_bookmarks(
          username TEXT NOT NULL,
          record_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(username,record_id)
        );
        ''')
        review_columns = {row['name'] for row in c.execute('PRAGMA table_info(company_reviews)')}
        if 'usage' not in review_columns:
            c.execute("ALTER TABLE company_reviews ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'")
        recording_columns = {row['name'] for row in c.execute('PRAGMA table_info(event_recordings)')}
        if 'error_stage' not in recording_columns:
            c.execute('ALTER TABLE event_recordings ADD COLUMN error_stage TEXT')
        if 'error_detail' not in recording_columns:
            c.execute('ALTER TABLE event_recordings ADD COLUMN error_detail TEXT')
        if 'process_log' not in recording_columns:
            c.execute("ALTER TABLE event_recordings ADD COLUMN process_log TEXT NOT NULL DEFAULT '[]'")
        c.execute("UPDATE sync_logs SET status='error',finished=?,message='服务重新启动，同步将重试' WHERE status='running'", (now(),))
        c.execute("UPDATE company_reviews SET status='queued',error='服务重新启动，分析将重试' WHERE status='running'")
        c.execute("UPDATE event_recordings SET status='queued',error='服务重新启动，录音处理将重试' WHERE status IN ('transcribing','summarizing')")
        if not c.execute('SELECT 1 FROM users').fetchone():
            password = os.getenv('ADMIN_PASSWORD') or secrets.token_urlsafe(24)
            c.execute('INSERT INTO users VALUES (?,?)', ('admin', hash_pw(password)))
            credentials = DATA / 'initial-admin.txt'
            credentials.write_text('管理地址：' + ORIGIN + '/admin\n用户名：admin\n初始密码：' + password + '\n登录后请在安全设置中修改密码。\n')
            credentials.chmod(0o600)
        if not c.execute("SELECT 1 FROM settings WHERE key='next_sync'").fetchone():
            put_setting(c, 'next_sync', time.time())
        c.execute("UPDATE settings SET value=? WHERE key='title' AND value=?",
                  (json.dumps('大潘的就业情报站', ensure_ascii=False),
                   json.dumps('就业情报站', ensure_ascii=False)))


def audit(c, action, record_id=''):
    c.execute('INSERT INTO audit(at,action,record_id) VALUES(?,?,?)', (now(), action, record_id))


def pushplus_status(cfg=None):
    cfg = cfg or settings()
    return {
        'configured': bool(os.getenv('PUSHPLUS_TOKEN', '').strip()),
        'enabled': bool(cfg.get('pushplus_enabled', True)),
        'last_success': cfg.get('pushplus_last_success'),
        'last_error': cfg.get('pushplus_last_error'),
    }


def send_pushplus(events, test=False):
    token = os.getenv('PUSHPLUS_TOKEN', '').strip()
    if not token:
        raise RuntimeError('PushPlus Token 尚未配置')
    blocks = [
        f"企业：{str(item.get('company') or '企业名称未注明').strip()}\n"
        f"时间：{str(item.get('time_text') or item.get('starts_at') or '时间未注明').strip()}"
        for item in events
    ]
    payload = json.dumps({
        'token': token,
        'title': 'PushPlus 测试消息' if test else '新增宣讲会信息',
        'content': '\n\n'.join(blocks),
        'template': 'txt',
    }, ensure_ascii=False).encode('utf-8')
    endpoint = os.getenv('PUSHPLUS_BASE_URL', '').strip() or 'https://www.pushplus.plus/send'
    request = urllib.request.Request(endpoint, data=payload, method='POST', headers={
        'Content-Type': 'application/json', 'User-Agent': 'JobBoard/1.0',
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError('PushPlus 连接失败') from exc
    if int(result.get('code', -1)) != 200:
        raise RuntimeError('PushPlus 推送失败：' + str(result.get('msg') or result.get('message') or '未知错误')[:160])
    return True


def source_record_key(item):
    """Stable content key used to collapse duplicate source rows."""
    if item.get('kind') == 'event':
        return ('event', item.get('company', '').strip(), item.get('starts_at') or item.get('time_text'),
                item.get('location', '').strip())
    return ('job', item.get('company', '').strip(), item.get('positions', '').strip(),
            item.get('apply_url') or item.get('announcement_url'), item.get('start_date'),
            item.get('deadline_date'), item.get('location', '').strip())


def source_reconcile_keys(item):
    """Keys that identify an existing row when links or event times are corrected."""
    if item.get('kind') == 'event':
        keys = [source_record_key(item)]
        if item.get('source_row'):
            keys.append(('event-row', item.get('company', '').strip(), item.get('source_sheet'), item.get('source_row')))
        if item.get('date') and item.get('location'):
            keys.append(('event-day', item.get('company', '').strip(), item.get('date'), item.get('location', '').strip()))
        return keys
    keys = [source_record_key(item)]
    if item.get('source_row'):
        keys.append(('job-row', item.get('company', '').strip(), item.get('source_sheet'), item.get('source_row')))
    if item.get('positions'):
        keys.append(('job-position', item.get('company', '').strip(), item.get('positions', '').strip(),
                     item.get('start_date'), item.get('deadline_date')))
    return keys


def run_sync():
    if not sync_lock.acquire(blocking=False):
        return
    logid = None
    try:
        cfg = settings()
        with conn() as c:
            logid = c.execute("INSERT INTO sync_logs(started,status) VALUES(?,'running')", (now(),)).lastrowid
        records, meta = fetch_source(cfg['source_url'])
        # The source may contain repeated rows. Keep one complete record for each
        # logical item before touching persistent history.
        records = list({source_record_key(item): item for item in records}.values())
        with conn() as c:
            deleted_ids = {row['id'] for row in c.execute('SELECT id FROM deleted_records')}
            deleted_keys = {row['source_key'] for row in c.execute(
                "SELECT source_key FROM deleted_records WHERE source='kdocs' AND source_key IS NOT NULL"
            )}
        records = [
            item for item in records
            if item['id'] not in deleted_ids
            and json.dumps(source_record_key(item), ensure_ascii=False) not in deleted_keys
        ]
        jobs = sum(x['kind'] == 'job' for x in records)
        events = len(records) - jobs
        with conn() as c:
            old = {r['id']: r['data'] for r in c.execute("SELECT id,data FROM records WHERE source='kdocs'")}
            old_items = [json.loads(v) for v in old.values()]
            old_event_keys = {source_record_key(item) for item in old_items if item.get('kind') == 'event'}
            new_events = [
                item for item in records
                if old_event_keys and item.get('kind') == 'event' and source_record_key(item) not in old_event_keys
            ]
            reconcile = {}
            for previous in old_items:
                for key in source_reconcile_keys(previous):
                    reconcile.setdefault(key, []).append(previous['id'])
            for item in records:
                if item['id'] in old:
                    continue
                for key in source_reconcile_keys(item):
                    candidates = reconcile.get(key, [])
                    if len(candidates) == 1:
                        item['id'] = candidates[0]
                        break
            records = [item for item in records if item['id'] not in deleted_ids]
            changed = 0
            for item in records:
                body = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if old.get(item['id']) != body:
                    changed += 1
                c.execute('INSERT INTO records VALUES(?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET data=excluded.data,present=1',
                          (item['id'], item['kind'], 'kdocs', body))
            put_setting(c, 'last_success', now())
            put_setting(c, 'source_meta', meta)
            put_setting(c, 'last_error', None)
            c.execute("UPDATE sync_logs SET finished=?,status='success',jobs=?,events=?,changed=?,message=? WHERE id=?",
                      (now(), jobs, events, changed, '同步完成' if changed else '检查完成，原表无变化', logid))
        if new_events and pushplus_status(cfg)['configured'] and pushplus_status(cfg)['enabled']:
            try:
                send_pushplus(new_events)
                with conn() as c:
                    put_setting(c, 'pushplus_last_success', now())
                    put_setting(c, 'pushplus_last_error', None)
                    audit(c, 'PushPlus 推送新增宣讲会', str(len(new_events)))
            except Exception as exc:
                with conn() as c:
                    put_setting(c, 'pushplus_last_error', str(exc)[:300])
                    c.execute("UPDATE sync_logs SET message=message||? WHERE id=?", ('；' + str(exc)[:180], logid))
                    audit(c, 'PushPlus 推送失败', str(exc)[:180])
    except Exception as e:
        message = str(e)[:400]
        with conn() as c:
            put_setting(c, 'last_error', message)
            if logid:
                c.execute("UPDATE sync_logs SET finished=?,status='error',message=? WHERE id=?", (now(), message, logid))
    finally:
        with conn() as c:
            put_setting(c, 'last_attempt', now())
            put_setting(c, 'next_sync', time.time() + INTERVAL)
            c.execute('DELETE FROM sync_logs WHERE id NOT IN (SELECT id FROM sync_logs ORDER BY id DESC LIMIT 500)')
        sync_lock.release()


def worker():
    while not stop.is_set():
        cfg = settings()
        if cfg['auto_sync'] and time.time() >= cfg.get('next_sync', 0):
            run_sync()
        wake.wait(5)
        wake.clear()


def review_dict(row, include_error=False):
    if not row:
        return None
    result = dict(row)
    for key in ('pros', 'cons', 'sources', 'usage'):
        try:
            result[key] = json.loads(result.get(key) or ('{}' if key == 'usage' else '[]'))
        except json.JSONDecodeError:
            result[key] = {} if key == 'usage' else []
    if not include_error:
        result.pop('error', None)
        result.pop('queued_at', None)
        result.pop('provider', None)
        result.pop('usage', None)
    return result


def process_review_queue():
    cfg = review.config()
    if not cfg['configured'] or not review_lock.acquire(blocking=False):
        return False
    company = None
    try:
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT company FROM company_reviews WHERE status='queued' ORDER BY queued_at LIMIT 1").fetchone()
            if not row:
                return False
            company = row['company']
            c.execute("UPDATE company_reviews SET status='running',error=NULL WHERE company=?", (company,))
        result, provider = review.analyze(company)
        with conn() as c:
            c.execute('''UPDATE company_reviews SET status='success',score=?,label=?,summary=?,pros=?,cons=?,sources=?,
                         provider=?,usage=?,updated_at=?,error=NULL WHERE company=?''',
                      (result['score'], result['label'], result['summary'],
                       json.dumps(result['pros'], ensure_ascii=False),
                       json.dumps(result['cons'], ensure_ascii=False),
                       json.dumps(result['sources'], ensure_ascii=False),
                       provider['name'] + ' / ' + provider['model'],
                       json.dumps(result.get('usage') or {}, ensure_ascii=False), now(), company))
            audit(c, '完成网评分析', company)
    except Exception as e:
        if company:
            with conn() as c:
                usage = getattr(e, 'usage', {}) or {}
                c.execute("UPDATE company_reviews SET status='error',error=?,usage=? WHERE company=?",
                          (str(e)[:500], json.dumps(usage, ensure_ascii=False), company))
                audit(c, '网评分析失败', company)
    finally:
        review_lock.release()
    return bool(company)


def review_worker():
    while not stop.is_set():
        if not process_review_queue():
            review_wake.wait(5)
            review_wake.clear()


def recording_item(record_id):
    for item in effective(include_invisible=True, include_archived=True, include_interviews=True):
        if item['id'] == record_id and item['kind'] in ('event', 'interview'):
            return item
    return None


def append_recording_log(record_id, stage, message):
    with conn() as c:
        row = c.execute('SELECT process_log FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if not row:
            return
        try:
            entries = json.loads(row['process_log'] or '[]')
        except (TypeError, json.JSONDecodeError):
            entries = []
        entries.append({'at': now(), 'stage': stage, 'message': str(message)[:500]})
        c.execute('UPDATE event_recordings SET process_log=? WHERE record_id=?',
                  (json.dumps(entries[-40:], ensure_ascii=False), record_id))


def update_recording_status(record_id, status, error=None, error_stage=None, error_detail=None, **values):
    allowed = {'asr_task_id', 'transcript', 'summary', 'source_token_hash', 'source_token_expires'}
    assignments = ['status=?', 'error=?', 'error_stage=?', 'error_detail=?', 'updated_at=?']
    params = [status, error, error_stage, error_detail, now()]
    for key, value in values.items():
        if key not in allowed:
            continue
        assignments.append(key + '=?')
        params.append(value)
    params.append(record_id)
    with conn() as c:
        c.execute(f"UPDATE event_recordings SET {','.join(assignments)} WHERE record_id=?", params)


def process_recording_queue():
    cfg = recording.config()
    if not cfg['configured'] or not recording_lock.acquire(blocking=False):
        return False
    record_id = None
    stage = '领取任务'
    try:
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM event_recordings WHERE status='queued' ORDER BY updated_at LIMIT 1").fetchone()
            if not row:
                return False
            record_id = row['record_id']
            resume_transcript = row['transcript'] if row['transcript'] and not row['summary'] else None
            if resume_transcript:
                c.execute("UPDATE event_recordings SET status='summarizing',error=NULL,error_stage=NULL,error_detail=NULL,source_token_hash=NULL,source_token_expires=NULL,updated_at=? WHERE record_id=?",
                          (now(), record_id))
            else:
                raw_token = secrets.token_urlsafe(40)
                token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                expires = time.time() + 86400
                c.execute("UPDATE event_recordings SET status='transcribing',error=NULL,error_stage=NULL,error_detail=NULL,source_token_hash=?,source_token_expires=?,updated_at=? WHERE record_id=?",
                          (token_hash, expires, now(), record_id))
        event = recording_item(record_id) or {}
        if resume_transcript:
            stage = '生成总结'
            append_recording_log(record_id, stage, f'检测到已保存的 {len(resume_transcript)} 字转写，跳过 ASR')
            transcript = resume_transcript
        else:
            stage = '准备音频'
            append_recording_log(record_id, stage, '开始生成 16 kHz 单声道 ASR 副本')
            origin = (os.getenv('ASR_PUBLIC_ORIGIN', '').strip() or ORIGIN).rstrip('/')
            source_url = f'{origin}/api/recordings/source/{raw_token}'
            recording.prepare_asr_audio(row['file_path'])
            append_recording_log(record_id, stage, '单声道 ASR 副本准备完成')
            stage = '提交转写'
            task_id = recording.submit_asr(source_url)
            append_recording_log(record_id, stage, f'百炼任务已提交（任务号尾号 {task_id[-8:]}）')
            update_recording_status(record_id, 'transcribing', asr_task_id=task_id)
            stage = '等待转写'
            output = recording.wait_asr(task_id)
            append_recording_log(record_id, stage, '百炼转写任务已完成，开始读取结果')
            transcript = recording.fetch_transcript(output)
            update_recording_status(record_id, 'summarizing', transcript=transcript,
                                    source_token_hash=None, source_token_expires=None)
            append_recording_log(record_id, stage, f'已保存 {len(transcript)} 字原始转写')
            stage = '生成总结'
        append_recording_log(record_id, stage, '开始调用总结模型')
        summary = recording.summarize(
            transcript,
            event.get('company', ''),
            event.get('time_text', ''),
            event.get('kind', 'event'),
        )
        update_recording_status(record_id, 'completed', transcript=transcript, summary=summary,
                                source_token_hash=None, source_token_expires=None)
        append_recording_log(record_id, '完成', f'处理完成，已保存 {len(summary)} 字 Markdown 总结')
        with conn() as c:
            audit(c, '完成面试录音转写与总结' if event.get('kind') == 'interview' else '完成宣讲会录音转写与总结', record_id)
    except Exception as exc:
        if record_id:
            detail = '\n'.join((
                f'失败时间：{now()}',
                f'失败阶段：{stage}',
                f'异常类型：{type(exc).__name__}',
                f'异常信息：{str(exc)[:2000]}',
                '',
                '堆栈摘要：',
                traceback.format_exc(limit=8)[-6000:],
            ))
            append_recording_log(record_id, stage, f'处理失败：{type(exc).__name__}: {str(exc)[:300]}')
            update_recording_status(record_id, 'error', str(exc)[:800], stage, detail,
                                    source_token_hash=None, source_token_expires=None)
            with conn() as c:
                audit(c, '宣讲会录音处理失败', record_id)
    finally:
        recording_lock.release()
    return bool(record_id)


def recording_worker():
    while not stop.is_set():
        if not process_recording_queue():
            recording_wake.wait(5)
            recording_wake.clear()


@asynccontextmanager
async def lifespan(app):
    init()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=review_worker, daemon=True).start()
    threading.Thread(target=recording_worker, daemon=True).start()
    yield
    stop.set()
    wake.set()
    review_wake.set()
    recording_wake.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def policy(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('origin') not in ({None} | ORIGINS) or request.headers.get('x-requested-with') != 'job-board':
            return Response('Invalid request origin', 403)
        try:
            length = int(request.headers.get('content-length', '0') or '0')
        except ValueError:
            return Response('Invalid content length', 400)
        is_audio_upload = request.method == 'POST' and re.fullmatch(r'/api/admin/(?:events|records)/[^/]+/recording', request.url.path)
        limit = MAX_AUDIO_BYTES if is_audio_upload else 100000
        if length > limit:
            return Response('Request too large', 413)
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def admin(request):
    token = request.cookies.get('job_session', '')
    with conn() as c:
        session = c.execute('SELECT * FROM sessions WHERE token=? AND expires>?',
                            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not session:
        raise HTTPException(401, '请先登录管理后台')
    return session['username']


def recording_dict(row, detail=False):
    if not row:
        return None
    result = {
        key: row[key] for key in (
            'record_id', 'original_name', 'mime_type', 'size', 'status', 'asr_task_id',
            'summary', 'summary_public', 'error', 'created_at', 'updated_at'
        )
    }
    result['summary_public'] = bool(result['summary_public'])
    if detail:
        result['transcript'] = row['transcript']
        result['error_stage'] = row['error_stage']
        result['error_detail'] = row['error_detail']
        try:
            result['process_log'] = json.loads(row['process_log'] or '[]')
        except (TypeError, json.JSONDecodeError):
            result['process_log'] = []
    return result


def effective(include_invisible=False, include_admin_recordings=False, include_archived=False, include_interviews=False):
    with conn() as c:
        overrides = {r['id']: json.loads(r['data']) for r in c.execute('SELECT * FROM overrides')}
        rows = c.execute('SELECT * FROM records WHERE present=1').fetchall()
        company_reviews = {r['company']: review_dict(r) for r in c.execute('SELECT * FROM company_reviews WHERE score IS NOT NULL AND summary IS NOT NULL')}
        recordings = {r['record_id']: r for r in c.execute('SELECT * FROM event_recordings')}
    result = []
    today = dt.datetime.now(TZ).date().isoformat()
    instant = now()
    for row in rows:
        item = json.loads(row['data'])
        override = dict(overrides.get(row['id'], {}))
        if 'hidden' in override and 'visitor_visible' not in override:
            override['visitor_visible'] = not override['hidden']
        item.update(override)
        item['modified'] = row['id'] in overrides
        item['review'] = company_reviews.get(item.get('company'))
        item['visitor_visible'] = bool(item.get('visitor_visible', not item.get('hidden', False)))
        item['archived'] = bool(item.get('archived', False))
        item['hidden'] = not item['visitor_visible']
        if item['kind'] == 'interview' and not include_interviews:
            continue
        if item['archived'] and not include_archived:
            continue
        if not item['visitor_visible'] and not include_invisible:
            continue
        if item['kind'] == 'job':
            deadline, start = item.get('deadline_date'), item.get('start_date')
            item['status'] = 'expired' if deadline and deadline < today else ('upcoming' if start and start > today else 'open' if deadline else 'unknown')
        elif item['kind'] in ('event', 'interview'):
            date, start, end = item.get('date'), item.get('starts_at'), item.get('ends_at')
            item['status'] = 'unknown' if not date else ('ended' if date < today or end and end < instant else 'today' if date == today else 'upcoming')
            if date == today and item.get('time_known') and start and start < instant and not end:
                item['status'] = 'started'
            recording_row = recordings.get(item['id'])
            if recording_row:
                if include_admin_recordings:
                    item['recording'] = recording_dict(recording_row)
                elif item['kind'] == 'event' and recording_row['summary_public'] and recording_row['summary']:
                    item['recording_summary'] = recording_row['summary']
        result.append(item)
    return result


@app.get('/api/health')
def health():
    with conn() as c:
        c.execute('SELECT 1').fetchone()
    return {'ok': True}


@app.get('/api/public')
def public(request: Request):
    cfg = settings()
    try:
        admin(request)
        is_admin = True
    except HTTPException:
        is_admin = False
    return {'records': effective(include_invisible=is_admin, include_interviews=is_admin),
            'config': {k: cfg[k] for k in DEFAULTS},
            'sync': {'last_success': cfg.get('last_success'),
                     'next_sync': dt.datetime.fromtimestamp(cfg['next_sync'], TZ).isoformat() if cfg['auto_sync'] else None,
                     'has_error': bool(cfg.get('last_error')), 'running': sync_lock.locked(), 'interval_minutes': 15,
                     'source_title': cfg.get('source_meta', {}).get('title'),
                     'source_version': cfg.get('source_meta', {}).get('version')}, 'server_time': now()}


@app.get('/api/session')
def session(request: Request):
    try:
        username = admin(request)
    except HTTPException:
        return {'admin': False}
    return {'admin': True, 'username': username}


@app.get('/api/admin/bookmarks')
def get_admin_bookmarks(request: Request):
    username = admin(request)
    with conn() as c:
        ids = [row['record_id'] for row in c.execute(
            'SELECT record_id FROM admin_bookmarks WHERE username=? ORDER BY created_at', (username,)
        )]
    return {'ids': ids}


@app.put('/api/admin/bookmarks/{record_id}')
def add_admin_bookmark(record_id: str, request: Request):
    username = admin(request)
    with conn() as c:
        if not c.execute('SELECT 1 FROM records WHERE id=? AND present=1', (record_id,)).fetchone():
            raise HTTPException(404, '信息不存在')
        c.execute('INSERT OR IGNORE INTO admin_bookmarks VALUES(?,?,?)', (username, record_id, now()))
    return {'ok': True}


@app.delete('/api/admin/bookmarks/{record_id}')
def delete_admin_bookmark(record_id: str, request: Request):
    username = admin(request)
    with conn() as c:
        c.execute('DELETE FROM admin_bookmarks WHERE username=? AND record_id=?', (username, record_id))
    return {'ok': True}


class Login(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    ip = request.headers.get('x-real-ip') or request.client.host
    with conn() as c:
        c.execute('DELETE FROM login_attempts WHERE at<?', (time.time() - 900,))
        if c.execute('SELECT count(*) FROM login_attempts WHERE ip=?', (ip,)).fetchone()[0] >= 10:
            raise HTTPException(429, '尝试次数过多，请 15 分钟后重试')
        row = c.execute('SELECT password FROM users WHERE username=?', (body.username,)).fetchone()
        if row:
            valid = check_pw(body.password, row['password'])
        else:
            # Keep password-hash work for unknown users, but never authenticate them.
            check_pw(body.password, hash_pw('dummy-password'))
            valid = False
        if not valid:
            c.execute('INSERT INTO login_attempts VALUES(?,?)', (ip, time.time()))
            c.commit()
            raise HTTPException(401, '用户名或密码错误')
        c.execute('DELETE FROM login_attempts WHERE ip=?', (ip,))
        token = secrets.token_urlsafe(40)
        c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), body.username, time.time() + 28800))
        audit(c, '管理员登录')
    response.set_cookie('job_session', token, httponly=True, secure=secure_request(request), samesite='strict', max_age=28800, path='/')
    return {'username': body.username}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with conn() as c:
        c.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(request.cookies.get('job_session', '').encode()).hexdigest(),))
    response.delete_cookie('job_session', path='/', secure=secure_request(request), httponly=True, samesite='strict')
    return {'ok': True}


@app.get('/api/admin')
def dashboard(request: Request):
    username = admin(request)
    cfg = settings()
    with conn() as c:
        logs = [dict(r) for r in c.execute('SELECT * FROM sync_logs ORDER BY id DESC LIMIT 100')]
        audits = [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 50')]
        stored_reviews = {r['company']: review_dict(r, True) for r in c.execute('SELECT * FROM company_reviews')}
    records = effective(include_invisible=True, include_admin_recordings=True,
                        include_archived=True, include_interviews=True)
    companies = sorted(reviewable_companies(records))
    reviews = [stored_reviews.get(company) or {'company': company, 'status': 'unreviewed', 'score': None,
                                               'label': None, 'summary': None, 'pros': [], 'cons': [],
                                               'sources': [], 'provider': None, 'updated_at': None,
                                               'queued_at': None, 'error': None}
               for company in companies]
    return {'username': username, 'records': records, 'config': {k: cfg[k] for k in DEFAULTS},
            'logs': logs, 'audit': audits, 'last_error': cfg.get('last_error'), 'sync_running': sync_lock.locked(),
            'reviews': reviews, 'review_config': review.config(), 'review_running': review_lock.locked(),
            'pushplus': pushplus_status(cfg), 'recording_config': recording.config(),
            'recording_running': recording_lock.locked()}


@app.post('/api/admin/sync')
def sync(request: Request):
    admin(request)
    if sync_lock.locked():
        raise HTTPException(409, '同步正在进行中')
    with conn() as c:
        last = c.execute('SELECT started FROM sync_logs ORDER BY id DESC LIMIT 1').fetchone()
        if last and (dt.datetime.now(TZ) - dt.datetime.fromisoformat(last['started'])).total_seconds() < 30:
            raise HTTPException(429, '请稍候 30 秒再重试')
        audit(c, '手动同步')
    threading.Thread(target=run_sync, daemon=True).start()
    return {'ok': True}


class ReviewRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)


class ReviewBatchRequest(BaseModel):
    mode: str = Field(pattern=r'^(missing|stale)$')
    max_items: int = Field(default=5, ge=1, le=5)


class PushPlusUpdate(BaseModel):
    enabled: bool


class RecordingVisibilityUpdate(BaseModel):
    public: bool


def require_recordable(record_id):
    item = recording_item(record_id)
    if not item:
        raise HTTPException(404, '宣讲会或面试记录不存在')
    return item


def safe_recording_path(value):
    try:
        path = Path(value).resolve()
        path.relative_to(RECORDINGS.resolve())
    except (ValueError, OSError):
        raise HTTPException(404, '录音文件不存在')
    if not path.is_file():
        raise HTTPException(404, '录音文件不存在')
    return path


@app.post('/api/admin/events/{record_id}/recording')
@app.post('/api/admin/records/{record_id}/recording')
async def upload_recording(record_id: str, request: Request):
    admin(request)
    subject = require_recordable(record_id)
    encoded_name = request.headers.get('x-file-name', '')
    try:
        from urllib.parse import unquote
        original_name = Path(unquote(encoded_name)).name
    except Exception:
        original_name = ''
    extension = Path(original_name).suffix.lower()
    if not original_name or extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(422, '请选择 M4A、MP3、MP4、WAV、AAC、FLAC、OGG 或 WebM 录音')
    with conn() as c:
        current = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if current and current['status'] in ('transcribing', 'summarizing'):
            raise HTTPException(409, '录音正在处理中，暂时不能替换')
    target_dir = RECORDINGS / hashlib.sha256(record_id.encode()).hexdigest()[:24]
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    target = target_dir / (uuid.uuid4().hex + extension)
    temporary = target.with_suffix(target.suffix + '.upload')
    total = 0
    try:
        with temporary.open('wb') as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise HTTPException(413, f'录音不能超过 {MAX_AUDIO_BYTES // 1024 // 1024} MB')
                output.write(chunk)
        if total == 0:
            raise HTTPException(422, '录音文件为空')
        temporary.replace(target)
        target.chmod(0o600)
        with conn() as c:
            current = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
            c.execute('''INSERT OR REPLACE INTO event_recordings(
                         record_id,original_name,file_path,mime_type,size,status,asr_task_id,transcript,summary,
                         summary_public,error,source_token_hash,source_token_expires,created_at,updated_at)
                         VALUES(?,?,?,?,?,'uploaded',NULL,NULL,NULL,0,NULL,NULL,NULL,?,?)''',
                      (record_id, original_name[:255], str(target), request.headers.get('content-type', '')[:120],
                       total, current['created_at'] if current else now(), now()))
            label = '面试' if subject['kind'] == 'interview' else '宣讲会'
            audit(c, f'{"上传" if not current else "重新上传"}{label}录音', record_id)
        if current:
            old_path = Path(current['file_path'])
            if old_path != target:
                try:
                    old_path.resolve().relative_to(RECORDINGS.resolve())
                    recording.asr_audio_path(old_path).unlink(missing_ok=True)
                    old_path.unlink(missing_ok=True)
                except (ValueError, OSError):
                    pass
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    with conn() as c:
        return recording_dict(c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone())


@app.get('/api/admin/events/{record_id}/recording')
@app.get('/api/admin/records/{record_id}/recording')
def get_recording(record_id: str, request: Request):
    admin(request)
    require_recordable(record_id)
    with conn() as c:
        row = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, '尚未上传录音')
    return recording_dict(row, True)


@app.get('/api/admin/events/{record_id}/recording/file')
@app.get('/api/admin/records/{record_id}/recording/file')
def download_recording(record_id: str, request: Request):
    admin(request)
    with conn() as c:
        row = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, '尚未上传录音')
    return FileResponse(safe_recording_path(row['file_path']), media_type=row['mime_type'] or 'application/octet-stream',
                        filename=row['original_name'])


def remove_recording_files(row):
    if not row:
        return
    try:
        path = safe_recording_path(row['file_path'])
        recording.asr_audio_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except (HTTPException, OSError):
        pass


@app.delete('/api/admin/events/{record_id}/recording')
@app.delete('/api/admin/records/{record_id}/recording')
def delete_recording(record_id: str, request: Request):
    admin(request)
    subject = require_recordable(record_id)
    with conn() as c:
        row = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '尚未上传录音')
        if row['status'] in ('transcribing', 'summarizing'):
            raise HTTPException(409, '录音正在处理中，暂时不能删除')
        c.execute('DELETE FROM event_recordings WHERE record_id=?', (record_id,))
        audit(c, '删除面试录音' if subject['kind'] == 'interview' else '删除宣讲会录音', record_id)
    remove_recording_files(row)
    return {'ok': True}


@app.post('/api/admin/events/{record_id}/recording/process')
@app.post('/api/admin/records/{record_id}/recording/process')
def start_recording_process(record_id: str, request: Request):
    admin(request)
    subject = require_recordable(record_id)
    cfg = recording.config()
    if not cfg['configured']:
        raise HTTPException(409, '录音处理服务尚未配置完整：' + '、'.join(cfg['missing']))
    with conn() as c:
        row = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '请先上传录音')
        if row['status'] in ('queued', 'transcribing', 'summarizing'):
            raise HTTPException(409, '这份录音已在处理队列中')
        c.execute("UPDATE event_recordings SET status='queued',error=NULL,error_stage=NULL,error_detail=NULL,updated_at=? WHERE record_id=?", (now(), record_id))
        audit(c, '启动面试录音处理' if subject['kind'] == 'interview' else '启动宣讲会录音处理', record_id)
    append_recording_log(record_id, '排队', '管理员已启动录音处理')
    recording_wake.set()
    return {'ok': True}


@app.put('/api/admin/events/{record_id}/recording/visibility')
@app.put('/api/admin/records/{record_id}/recording/visibility')
def update_recording_visibility(record_id: str, body: RecordingVisibilityUpdate, request: Request):
    admin(request)
    if require_recordable(record_id)['kind'] != 'event':
        raise HTTPException(409, '面试记录仅管理员可见，无需设置公开总结')
    with conn() as c:
        row = c.execute('SELECT summary FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '尚未上传录音')
        if body.public and not row['summary']:
            raise HTTPException(409, '总结完成后才能公开')
        c.execute('UPDATE event_recordings SET summary_public=?,updated_at=? WHERE record_id=?',
                  (int(body.public), now(), record_id))
        audit(c, '公开宣讲会总结' if body.public else '隐藏宣讲会总结', record_id)
    return {'ok': True, 'public': body.public}


@app.get('/api/recordings/source/{token}')
def asr_recording_source(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with conn() as c:
        row = c.execute('''SELECT * FROM event_recordings
                           WHERE source_token_hash=? AND source_token_expires>? AND status='transcribing' ''',
                        (token_hash, time.time())).fetchone()
    if not row or not hmac.compare_digest(row['source_token_hash'], token_hash):
        raise HTTPException(404, '临时录音链接不存在或已过期')
    return FileResponse(safe_recording_path(recording.asr_audio_path(row['file_path'])), media_type='audio/mp4')


@app.put('/api/admin/pushplus')
def update_pushplus(body: PushPlusUpdate, request: Request):
    admin(request)
    with conn() as c:
        put_setting(c, 'pushplus_enabled', body.enabled)
        audit(c, '启用 PushPlus 推送' if body.enabled else '暂停 PushPlus 推送')
    return {'ok': True, 'enabled': body.enabled}


@app.post('/api/admin/pushplus/test')
def test_pushplus(request: Request):
    admin(request)
    try:
        send_pushplus([{'company': '测试企业', 'time_text': now()}], test=True)
    except Exception as exc:
        with conn() as c:
            put_setting(c, 'pushplus_last_error', str(exc)[:300])
            audit(c, 'PushPlus 测试失败', str(exc)[:180])
        raise HTTPException(502, str(exc))
    with conn() as c:
        put_setting(c, 'pushplus_last_success', now())
        put_setting(c, 'pushplus_last_error', None)
        audit(c, '发送 PushPlus 测试消息')
    return {'ok': True}


def require_review_config():
    cfg = review.config()
    if not cfg['configured']:
        raise HTTPException(409, '请先在服务器环境变量中配置网评搜索与大模型服务')
    return cfg


def known_companies():
    return reviewable_companies()


def reviewable_companies(records=None):
    """Companies with a job posting or a presentation that has not ended."""
    records = records if records is not None else effective(True)
    return {
        item.get('company', '').strip()
        for item in records
        if item.get('company', '').strip()
        and (item.get('kind') == 'job' or item.get('kind') == 'event' and item.get('status') != 'ended')
    }


def queue_review(c, company):
    c.execute('''INSERT INTO company_reviews(company,status,queued_at,error) VALUES(?,'queued',?,NULL)
                 ON CONFLICT(company) DO UPDATE SET status='queued',queued_at=excluded.queued_at,error=NULL''',
              (company, now()))


@app.post('/api/admin/reviews/analyze')
def analyze_review(body: ReviewRequest, request: Request):
    admin(request)
    require_review_config()
    company = body.company.strip()
    if company not in known_companies():
        raise HTTPException(404, '当前招聘或宣讲信息中没有这家公司')
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT status FROM company_reviews WHERE company=?', (company,)).fetchone()
        if current and current['status'] in ('queued', 'running'):
            raise HTTPException(409, '这家公司的分析任务已在队列中')
        pending = c.execute(
            "SELECT COUNT(*) FROM company_reviews WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if pending >= MAX_REVIEW_PENDING:
            raise HTTPException(409, f'分析队列最多保留 {MAX_REVIEW_PENDING} 条任务，请等待当前任务完成')
        queue_review(c, company)
        audit(c, '提交网评分析', company)
    review_wake.set()
    return {'ok': True, 'queued': 1}


@app.post('/api/admin/reviews/batch')
def batch_reviews(body: ReviewBatchRequest, request: Request):
    admin(request)
    require_review_config()
    companies = sorted(known_companies())
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=30)).isoformat(timespec='seconds')
    queued = []
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        pending = c.execute(
            "SELECT COUNT(*) FROM company_reviews WHERE status IN ('queued','running')"
        ).fetchone()[0]
        available = max(0, MAX_REVIEW_PENDING - pending)
        existing = {r['company']: dict(r) for r in c.execute('SELECT company,status,score,updated_at FROM company_reviews')}
        for company in companies:
            row = existing.get(company)
            if row and row['status'] in ('queued', 'running'):
                continue
            eligible = (
                body.mode == 'missing' and (not row or row['score'] is None)
                or body.mode == 'stale' and row and row['score'] is not None and (not row['updated_at'] or row['updated_at'] < cutoff)
            )
            if eligible and len(queued) < min(body.max_items, available):
                queue_review(c, company)
                queued.append(company)
                if len(queued) >= min(body.max_items, available):
                    break
        audit(c, '批量提交网评分析', str(len(queued)))
    if queued:
        review_wake.set()
    return {'ok': True, 'queued': len(queued)}


class SettingsUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    subtitle: str = Field(max_length=160)
    announcement: str = Field(max_length=1500)
    source_url: str = Field(pattern=r'^https://www\.kdocs\.cn/l/[A-Za-z0-9]+$')
    auto_sync: bool


@app.put('/api/admin/settings')
def update_settings(body: SettingsUpdate, request: Request):
    admin(request)
    old = settings()
    if sync_lock.locked() and body.source_url != old['source_url']:
        raise HTTPException(409, '同步过程中暂不能更换数据源')
    with conn() as c:
        for k, v in body.model_dump().items():
            put_setting(c, k, v)
        if body.auto_sync and not old['auto_sync'] or body.source_url != old['source_url']:
            put_setting(c, 'next_sync', time.time())
        audit(c, '修改站点设置')
    wake.set()
    return {'ok': True}


ALLOWED = {'company', 'category', 'positions', 'location', 'education', 'salary', 'method', 'application',
           'apply_url', 'announcement_url', 'notes', 'updated', 'deadline', 'start', 'time_text',
           'visitor_visible', 'archived', 'hidden', 'pinned', 'interview_stage', 'result'}


def validated_patch(patch, kind):
    if set(patch) - ALLOWED:
        raise HTTPException(422, '包含不允许修改的字段')
    for k, v in patch.items():
        if k in ('hidden', 'visitor_visible', 'archived', 'pinned'):
            if not isinstance(v, bool):
                raise HTTPException(422, '状态必须为布尔值')
        elif not isinstance(v, str) or len(v) > 10000:
            raise HTTPException(422, '字段内容不合法或过长')
        if k in ('apply_url', 'announcement_url') and v and not (v.startswith(('https://', 'http://', 'mailto:')) and safe_link(v)):
            raise HTTPException(422, '链接仅允许 http、https 或 mailto')
    if 'company' in patch and not patch['company'].strip():
        raise HTTPException(422, '单位名称不能为空')
    year = dt.datetime.now(TZ).year
    for k in ['deadline', 'start', 'updated']:
        if k in patch:
            parsed = normalize_date(patch[k], year)
            if k != 'updated' and patch[k] and not parsed:
                raise HTTPException(422, '日期请使用 YYYY-MM-DD 格式')
            patch[k + '_date'] = parsed
    if 'time_text' in patch:
        value = normalize_date(patch['time_text'], year, True)
        if not value:
            raise HTTPException(422, '时间请包含完整日期')
        patch.update(starts_at=value, date=value[:10], time_known=bool(re.search(r'\d[:：]\d', patch['time_text'])), ends_at=None)
        times = re.findall(r'(\d{1,2})\s*[:：]\s*(\d{2})', patch['time_text'])
        if len(times) > 1:
            try:
                patch['ends_at'] = dt.datetime.fromisoformat(value).replace(hour=int(times[1][0]), minute=int(times[1][1])).isoformat()
            except ValueError:
                raise HTTPException(422, '结束时间不正确')
    return patch


@app.patch('/api/admin/records/{record_id}')
def patch_record(record_id: str, body: dict, request: Request):
    admin(request)
    with conn() as c:
        row = c.execute('SELECT * FROM records WHERE id=? AND present=1', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '记录不存在')
        patch = validated_patch(body, row['kind'])
        old = c.execute('SELECT data FROM overrides WHERE id=?', (record_id,)).fetchone()
        merged = (json.loads(old['data']) if old else {}) | patch
        c.execute('INSERT OR REPLACE INTO overrides VALUES(?,?)', (record_id, json.dumps(merged, ensure_ascii=False)))
        audit(c, '调整信息', record_id)
    return {'ok': True}


@app.delete('/api/admin/records/{record_id}/override')
def reset_record(record_id: str, request: Request):
    admin(request)
    with conn() as c:
        c.execute('DELETE FROM overrides WHERE id=?', (record_id,))
        audit(c, '恢复原表内容', record_id)
    return {'ok': True}


@app.delete('/api/admin/records/{record_id}')
def delete_record(record_id: str, request: Request):
    admin(request)
    recording_row = None
    with conn() as c:
        row = c.execute('SELECT * FROM records WHERE id=? AND present=1', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '记录不存在')
        recording_row = c.execute('SELECT * FROM event_recordings WHERE record_id=?', (record_id,)).fetchone()
        if recording_row and recording_row['status'] in ('queued', 'transcribing', 'summarizing'):
            raise HTTPException(409, '录音正在处理中，暂时不能删除这条记录')
        item = json.loads(row['data'])
        if row['source'] == 'kdocs':
            c.execute('INSERT OR REPLACE INTO deleted_records VALUES(?,?,?,?)',
                      (record_id, row['source'], json.dumps(source_record_key(item), ensure_ascii=False), now()))
            c.execute('UPDATE records SET present=0 WHERE id=?', (record_id,))
        else:
            c.execute('DELETE FROM records WHERE id=?', (record_id,))
        c.execute('DELETE FROM overrides WHERE id=?', (record_id,))
        c.execute('DELETE FROM admin_bookmarks WHERE record_id=?', (record_id,))
        c.execute('DELETE FROM event_recordings WHERE record_id=?', (record_id,))
        audit(c, '删除信息', record_id)
    remove_recording_files(recording_row)
    return {'ok': True}


@app.post('/api/admin/records')
def add_record(body: dict, request: Request):
    admin(request)
    kind = body.pop('kind', None)
    if kind not in ('job', 'event', 'interview'):
        raise HTTPException(422, '类型不正确')
    if not body.get('company') or kind in ('event', 'interview') and not body.get('time_text'):
        raise HTTPException(422, '请填写名称和必要的时间')
    patch = validated_patch(body, kind)
    rid = 'manual-' + uuid.uuid4().hex
    record = {'id': rid, 'kind': kind, 'source': 'manual', 'source_sheet': '人工补充', 'source_row': None,
              'hidden': kind == 'interview', 'visitor_visible': kind != 'interview', 'archived': False,
              'pinned': False, 'updated': dt.datetime.now(TZ).date().isoformat(), **patch}
    with conn() as c:
        c.execute('INSERT INTO records VALUES(?,?,?,?,1)', (rid, kind, 'manual', json.dumps(record, ensure_ascii=False)))
        audit(c, '新增信息', rid)
    return {'id': rid}


class PasswordUpdate(BaseModel):
    current: str = Field(max_length=256)
    password: str = Field(min_length=12, max_length=256)


@app.put('/api/admin/password')
def change_password(body: PasswordUpdate, request: Request, response: Response):
    username = admin(request)
    with conn() as c:
        old = c.execute('SELECT password FROM users WHERE username=?', (username,)).fetchone()['password']
        if not check_pw(body.current, old):
            raise HTTPException(400, '当前密码不正确')
        c.execute('UPDATE users SET password=? WHERE username=?', (hash_pw(body.password), username))
        c.execute('DELETE FROM sessions WHERE username=?', (username,))
        audit(c, '修改管理员密码')
    (DATA / 'initial-admin.txt').unlink(missing_ok=True)
    response.delete_cookie('job_session', path='/', secure=secure_request(request), httponly=True, samesite='strict')
    return {'ok': True}


DIST = Path(os.getenv('DIST_DIR', '/app/frontend/dist'))
if (DIST / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/favicon.svg')
def favicon():
    return FileResponse(DIST / 'favicon.svg')


@app.api_route('/', methods=['GET', 'HEAD'])
@app.api_route('/admin', methods=['GET', 'HEAD'])
def index():
    return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-cache'})
