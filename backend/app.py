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
import unicodedata
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from source import SOURCE, TZ, fetch_source, safe_link, normalize_date
import review
import recording
import feishu

DATA = Path(os.getenv('DATA_DIR', '/app/data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'jobs.sqlite3'
ORIGIN = os.getenv('APP_ORIGIN', 'http://localhost:8000')
ORIGINS = {ORIGIN, *(x.strip() for x in os.getenv('APP_ORIGINS', '').split(',') if x.strip())}
INTERVAL = 900
FEISHU_INTERVAL = max(60, int(os.getenv('FEISHU_SYNC_INTERVAL_SECONDS', '3600')))
wake = threading.Event()
stop = threading.Event()
sync_lock = threading.Lock()
review_lock = threading.Lock()
review_wake = threading.Event()
recording_lock = threading.Lock()
recording_wake = threading.Event()
feishu_lock = threading.Lock()
feishu_wake = threading.Event()
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
        CREATE TABLE IF NOT EXISTS feishu_tables(
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          name TEXT,
          payload TEXT NOT NULL,
          synced_at TEXT NOT NULL,
          PRIMARY KEY(base_token, table_id)
        );
        CREATE TABLE IF NOT EXISTS feishu_fields(
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          field_id TEXT NOT NULL,
          name TEXT,
          type INTEGER,
          payload TEXT NOT NULL,
          synced_at TEXT NOT NULL,
          PRIMARY KEY(base_token, table_id, field_id)
        );
        CREATE TABLE IF NOT EXISTS feishu_views(
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          view_id TEXT NOT NULL,
          name TEXT,
          payload TEXT NOT NULL,
          synced_at TEXT NOT NULL,
          PRIMARY KEY(base_token, table_id, view_id)
        );
        CREATE TABLE IF NOT EXISTS feishu_records(
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          payload TEXT NOT NULL,
          first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL,
          source_missing INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(base_token, table_id, record_id)
        );
        CREATE TABLE IF NOT EXISTS feishu_sync_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started TEXT NOT NULL,
          finished TEXT,
          status TEXT NOT NULL,
          tables INTEGER NOT NULL DEFAULT 0,
          records INTEGER NOT NULL DEFAULT 0,
          message TEXT
        );
        CREATE TABLE IF NOT EXISTS feishu_annotations(
          username TEXT NOT NULL,
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT '待筛选',
          priority INTEGER NOT NULL DEFAULT 0,
          tags TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          PRIMARY KEY(username, base_token, table_id, record_id)
        );
        CREATE TABLE IF NOT EXISTS feishu_record_search(
          base_token TEXT NOT NULL,
          table_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          company TEXT NOT NULL DEFAULT '',
          position TEXT NOT NULL DEFAULT '',
          industry TEXT NOT NULL DEFAULT '',
          company_type TEXT NOT NULL DEFAULT '',
          recruitment_type TEXT NOT NULL DEFAULT '',
          target TEXT NOT NULL DEFAULT '',
          location TEXT NOT NULL DEFAULT '',
          education TEXT NOT NULL DEFAULT '',
          exam TEXT NOT NULL DEFAULT '',
          deadline TEXT NOT NULL DEFAULT '',
          deadline_label TEXT NOT NULL DEFAULT '',
          deadline_sort TEXT NOT NULL DEFAULT '',
          updated_date TEXT NOT NULL DEFAULT '',
          search_text TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(base_token, table_id, record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_feishu_search_updated ON feishu_record_search(base_token,updated_date);
        CREATE INDEX IF NOT EXISTS idx_feishu_search_company ON feishu_record_search(base_token,company);
        CREATE INDEX IF NOT EXISTS idx_feishu_annotation_status ON feishu_annotations(username,base_token,status,priority);
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
        search_columns = {row['name'] for row in c.execute('PRAGMA table_info(feishu_record_search)')}
        if 'deadline_label' not in search_columns:
            c.execute("ALTER TABLE feishu_record_search ADD COLUMN deadline_label TEXT NOT NULL DEFAULT ''")
        if 'deadline_sort' not in search_columns:
            c.execute("ALTER TABLE feishu_record_search ADD COLUMN deadline_sort TEXT NOT NULL DEFAULT ''")
        c.execute('CREATE INDEX IF NOT EXISTS idx_feishu_search_deadline ON feishu_record_search(base_token,deadline_sort)')
        c.execute("UPDATE sync_logs SET status='error',finished=?,message='服务重新启动，同步将重试' WHERE status='running'", (now(),))
        c.execute("UPDATE company_reviews SET status='queued',error='服务重新启动，分析将重试' WHERE status='running'")
        c.execute("UPDATE event_recordings SET status='queued',error='服务重新启动，录音处理将重试' WHERE status IN ('transcribing','summarizing')")
        c.execute("UPDATE feishu_sync_runs SET status='error',finished=?,message='服务重新启动，同步未完成；旧数据已保留' WHERE status='running'", (now(),))
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


FEISHU_INDEX_COLUMNS = ('company', 'position', 'industry', 'company_type', 'recruitment_type',
                        'target', 'location', 'education', 'exam', 'deadline', 'deadline_label',
                        'deadline_sort', 'updated_date', 'search_text')


def normalize_feishu_deadline(value, reference_year=None):
    """Normalize messy human-entered deadline text without an LLM.

    The original value remains in ``deadline``. ``deadline_label`` is the
    readable value and ``deadline_sort`` is an ISO date key. Urgent phrases
    sort before calendar dates; unknown phrases sort after them.
    """
    raw = unicodedata.normalize('NFKC', str(value or '')).strip()
    raw = re.sub(r'\s+', ' ', raw)
    if not raw:
        return '', '未填写', '9999-12-31'
    if re.search(r'尽快|越早|尽早|及时', raw):
        return raw, '尽快投递', '0000-01-01'
    if re.search(r'招满|额满|名额满', raw):
        return raw, '招满即止', '0000-01-02'
    year_hint = int(reference_year or dt.datetime.now(TZ).year)
    match_year = re.search(r'(?<!\d)(20\d{2})(?!\d)', raw)
    if match_year:
        year_hint = int(match_year.group(1))
    elif re.match(r'^\d{2}[./-]', raw):
        year_hint = 2000 + int(raw[:2])
    # Convert common Chinese and duplicated-separator forms to a stable shape.
    text = raw.replace('截止', '').replace('之前', '').replace('前', '')
    text = re.sub(r'年\s*', '/', text)
    text = re.sub(r'月\s*', '/', text)
    text = re.sub(r'日', '', text)
    text = re.sub(r'[.．。／\\-]+', '/', text)
    text = re.sub(r'/+', '/', text).strip('/')
    text = re.sub(r'^(20\d{2})/(\d)(\d{2})$', r'\1/\2/\3', text)
    compact = re.search(r'(20\d{2})(\d{1,2})/(\d{1,2})', text)
    match = None if compact else re.search(r'(20\d{2}|\d{2})/(\d{1,2})(?:/(\d{1,2}))?', text)
    month_only = False
    if not match:
        # Values such as 202610/9 contain a four-digit year followed by a
        # compact month before the separator.
        if compact:
            year_hint, month, day = int(compact.group(1)), int(compact.group(2)), int(compact.group(3))
        else:
            month_day = re.search(r'(?<!\d)(\d{1,2})/(\d{1,2})', text)
            if not month_day:
                month_only = re.search(r'(?<!\d)(\d{1,2})/(?=月?底|月?末|月?初|上旬|中旬|下旬)', text)
                if not month_only:
                    return raw, raw, '9999-12-30'
                month, day = int(month_only.group(1)), 1
            else:
                month, day = int(month_day.group(1)), int(month_day.group(2))
    else:
        year_hint = int(match.group(1))
        if year_hint < 100:
            year_hint += 2000
        month = int(match.group(2))
        day = int(match.group(3) or 1)
        if not match.group(3):
            month_only = True
            # Month-only values represent the last known day of that month.
            day = 31
            while day > 28:
                try:
                    dt.date(year_hint, month, day)
                    break
                except ValueError:
                    day -= 1
    if re.search(r'月底|月末', raw):
        day = 31
        while day > 28:
            try:
                dt.date(year_hint, month, day)
                break
            except ValueError:
                day -= 1
    elif re.search(r'月初|上旬', raw):
        day = 1
    elif re.search(r'中旬', raw):
        day = 15
    elif re.search(r'下旬', raw):
        day = 25
    try:
        parsed = dt.date(year_hint, month, day)
    except (ValueError, TypeError):
        return raw, raw, '9999-12-30'
    qualifier = ' · 约' if re.search(r'月初|上旬|中旬|下旬|月底|月末', raw) else ' · 仅到月' if month_only else ''
    return raw, parsed.isoformat() + qualifier, parsed.isoformat()


def index_feishu_record(c, base, table_id, record_id, record):
    fields = record.get('fields') or {}
    def pick(*names):
        return next((str(fields[name]).strip() for name in names if fields.get(name) not in (None, '')), '')
    values = {
        'company': pick('公司名称', '企业名称', '单位名称'),
        'position': pick('校招岗位', '招聘岗位', '岗位', '职位'),
        'industry': pick('公司行业', '行业'),
        'company_type': pick('企业性质', '单位性质'),
        'recruitment_type': pick('招聘类型', '类型'),
        'target': pick('招聘对象', '目标人群'),
        'location': pick('工作地点', '工作城市'),
        'education': pick('学历', '学历要求'),
        'exam': pick('是否笔试', '笔试'),
        'deadline': pick('网申截止', '截止时间', '截止日期'),
        'updated_date': pick('网申更新', '更新时间', '更新日期'),
    }
    updated_year = re.search(r'(20\d{2})', values['updated_date'])
    raw_deadline, deadline_label, deadline_sort = normalize_feishu_deadline(
        values['deadline'], updated_year.group(1) if updated_year else None)
    values['deadline'] = raw_deadline
    values['deadline_label'] = deadline_label
    values['deadline_sort'] = deadline_sort
    values['search_text'] = unicodedata.normalize('NFKC', ' '.join(str(v) for v in fields.values())).casefold()
    c.execute('''INSERT INTO feishu_record_search
      (base_token,table_id,record_id,company,position,industry,company_type,recruitment_type,target,location,education,exam,deadline,deadline_label,deadline_sort,updated_date,search_text)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(base_token,table_id,record_id) DO UPDATE SET
      company=excluded.company,position=excluded.position,industry=excluded.industry,
      company_type=excluded.company_type,recruitment_type=excluded.recruitment_type,
      target=excluded.target,location=excluded.location,education=excluded.education,
      exam=excluded.exam,deadline=excluded.deadline,deadline_label=excluded.deadline_label,
      deadline_sort=excluded.deadline_sort,updated_date=excluded.updated_date,
      search_text=excluded.search_text''',
              (base, table_id, record_id, *(values[k] for k in FEISHU_INDEX_COLUMNS)))


def ensure_feishu_search_index():
    """Upgrade an existing raw mirror without making another 10k-row network request."""
    with conn() as c:
        rows = c.execute('''SELECT r.base_token,r.table_id,r.record_id,r.payload FROM feishu_records r
            LEFT JOIN feishu_record_search s ON s.base_token=r.base_token AND s.table_id=r.table_id AND s.record_id=r.record_id
            WHERE r.source_missing=0 AND (s.record_id IS NULL OR s.deadline_sort IS NULL OR s.deadline_sort='')''').fetchall()
        for row in rows:
            index_feishu_record(c, row['base_token'], row['table_id'], row['record_id'], json.loads(row['payload']))


def run_feishu_sync():
    if not feishu_lock.acquire(blocking=False):
        return False
    started = now()
    with conn() as c:
        run_id = c.execute("INSERT INTO feishu_sync_runs(started,status) VALUES(?,'running')", (started,)).lastrowid
    try:
        snap = feishu.snapshot()
        if not snap['tables']:
            raise feishu.FeishuError('飞书 Base 未返回任何数据表，旧数据已保留')
        base = snap['base_token']
        stamp = now()
        count = 0
        # Fetch all pages before this transaction; an API failure cannot partially replace the mirror.
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM feishu_tables WHERE base_token=?', (base,))
            c.execute('DELETE FROM feishu_fields WHERE base_token=?', (base,))
            c.execute('DELETE FROM feishu_views WHERE base_token=?', (base,))
            c.execute('UPDATE feishu_records SET source_missing=1 WHERE base_token=?', (base,))
            for entry in snap['tables']:
                table = entry['table']
                table_id = table.get('table_id') or table.get('id')
                if not table_id:
                    raise feishu.FeishuError('飞书数据表缺少 table_id')
                c.execute('INSERT INTO feishu_tables VALUES(?,?,?,?,?)',
                          (base, table_id, table.get('name'), json.dumps(table, ensure_ascii=False), stamp))
                for field in entry['fields']:
                    field_id = field.get('field_id') or field.get('id')
                    if not field_id:
                        raise feishu.FeishuError(f'数据表 {table_id} 的字段缺少 field_id')
                    c.execute('INSERT INTO feishu_fields VALUES(?,?,?,?,?,?,?)',
                              (base, table_id, field_id, field.get('field_name') or field.get('name'),
                               field.get('type'), json.dumps(field, ensure_ascii=False), stamp))
                for view in entry['views']:
                    view_id = view.get('view_id') or view.get('id')
                    if not view_id:
                        raise feishu.FeishuError(f'数据表 {table_id} 的视图缺少 view_id')
                    c.execute('INSERT INTO feishu_views VALUES(?,?,?,?,?,?)',
                              (base, table_id, view_id, view.get('view_name') or view.get('name'),
                               json.dumps(view, ensure_ascii=False), stamp))
                for record in entry['records']:
                    record_id = record.get('record_id') or record.get('id')
                    if not record_id:
                        raise feishu.FeishuError(f'数据表 {table_id} 的记录缺少 record_id')
                    c.execute('''INSERT INTO feishu_records VALUES(?,?,?,?,?,?,0)
                      ON CONFLICT(base_token,table_id,record_id) DO UPDATE SET
                      payload=excluded.payload,last_seen=excluded.last_seen,source_missing=0''',
                              (base, table_id, record_id, json.dumps(record, ensure_ascii=False), stamp, stamp))
                    index_feishu_record(c, base, table_id, record_id, record)
                    count += 1
            c.execute("UPDATE feishu_sync_runs SET finished=?,status='success',tables=?,records=?,message=? WHERE id=?",
                      (stamp, len(snap['tables']), count, '全量读取成功', run_id))
        return True
    except Exception as exc:
        with conn() as c:
            c.execute("UPDATE feishu_sync_runs SET finished=?,status='error',message=? WHERE id=?",
                      (now(), str(exc)[:800], run_id))
        return False
    finally:
        feishu_lock.release()
        feishu_wake.set()


def feishu_worker():
    # The Feishu source is optional and independent of KDocs.
    while not stop.is_set():
        delay = FEISHU_INTERVAL
        if feishu.public_config()['configured']:
            with conn() as c:
                last = c.execute('SELECT started,finished,status FROM feishu_sync_runs ORDER BY id DESC LIMIT 1').fetchone()
            if not last:
                delay = 0
            elif last['status'] == 'running':
                delay = 30
            else:
                stamp = last['finished'] or last['started']
                delay = max(0, FEISHU_INTERVAL - (time.time() - dt.datetime.fromisoformat(stamp).timestamp()))
            if delay <= 0:
                if feishu_lock.locked():
                    feishu_wake.wait(10)
                    feishu_wake.clear()
                else:
                    run_feishu_sync()
                continue
        feishu_wake.wait(delay)
        feishu_wake.clear()


@asynccontextmanager
async def lifespan(app):
    init()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=review_worker, daemon=True).start()
    threading.Thread(target=recording_worker, daemon=True).start()
    threading.Thread(target=feishu_worker, daemon=True).start()
    yield
    stop.set()
    wake.set()
    review_wake.set()
    recording_wake.set()
    feishu_wake.set()


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
            if item['kind'] == 'event':
                # Some source rows include an end time while others only include a start time.
                # Keep the end time as event detail, but do not let that optional field create a
                # different public status for otherwise equivalent presentations.
                item['status'] = 'unknown' if not date else (
                    'started' if date < today or date == today and item.get('time_known') and start and start <= instant
                    else 'today' if date == today else 'upcoming'
                )
            else:
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


def feishu_status_payload():
    cfg = feishu.public_config()
    base = feishu.target_config()['base_token']
    with conn() as c:
        last = c.execute('SELECT * FROM feishu_sync_runs ORDER BY id DESC LIMIT 1').fetchone()
        counts = c.execute('''SELECT count(*) AS records,
          count(DISTINCT table_id) AS tables FROM feishu_records
          WHERE base_token=? AND source_missing=0''', (base,)).fetchone()
    return {
        **cfg,
        'running': feishu_lock.locked(),
        'interval_minutes': FEISHU_INTERVAL // 60,
        'tables': int(counts['tables'] or 0),
        'records': int(counts['records'] or 0),
        'last_run': dict(last) if last else None,
    }


@app.get('/api/admin/feishu/status')
def feishu_status(request: Request):
    admin(request)
    return feishu_status_payload()


@app.post('/api/admin/feishu/sync')
def feishu_sync(request: Request):
    admin(request)
    if not feishu.public_config()['configured']:
        raise HTTPException(400, '飞书公开视图尚未配置，请设置 FEISHU_BASE_URL、FEISHU_TABLE_ID 和 FEISHU_VIEW_ID')
    if feishu_lock.locked():
        raise HTTPException(409, '飞书全量同步正在进行中')
    threading.Thread(target=run_feishu_sync, daemon=True).start()
    return {'ok': True}


@app.get('/api/admin/feishu/manifest')
def feishu_manifest(request: Request):
    admin(request)
    base = feishu.target_config()['base_token']
    with conn() as c:
        tables = [dict(r) for r in c.execute(
            'SELECT base_token,table_id,name,synced_at FROM feishu_tables WHERE base_token=? ORDER BY table_id', (base,))]
        for table in tables:
            table['fields'] = [dict(r) for r in c.execute(
                'SELECT field_id,name,type,payload FROM feishu_fields WHERE base_token=? AND table_id=? ORDER BY field_id',
                (base, table['table_id']))]
            table['views'] = [dict(r) for r in c.execute(
                'SELECT view_id,name,payload FROM feishu_views WHERE base_token=? AND table_id=? ORDER BY view_id',
                (base, table['table_id']))]
            table['records'] = c.execute(
                'SELECT count(*) FROM feishu_records WHERE base_token=? AND table_id=? AND source_missing=0',
                (base, table['table_id'])).fetchone()[0]
    return {'base_token': base, 'tables': tables, 'status': feishu_status_payload()}


@app.get('/api/admin/feishu/records')
def feishu_records(request: Request, table_id: str = '', q: str = '', limit: int = 100, offset: int = 0,
                   status: list[str] = Query(default=[]), priority: int = -1,
                   industry: list[str] = Query(default=[]), company_type: list[str] = Query(default=[]),
                   recruitment_type: list[str] = Query(default=[]), location: list[str] = Query(default=[]),
                   education: list[str] = Query(default=[]), exam: list[str] = Query(default=[]),
                   tag: list[str] = Query(default=[]), sort: str = 'updated_date', direction: str = 'desc',
                   group_by: str = '', focus: list[int] = Query(default=[])):
    username = admin(request)
    ensure_feishu_search_index()
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    base = feishu.target_config()['base_token']
    allowed_filters = {'industry': industry, 'company_type': company_type, 'recruitment_type': recruitment_type,
                       'location': location, 'education': education, 'exam': exam}
    sort_columns = {'company': 's.company COLLATE NOCASE', 'position': 's.position COLLATE NOCASE',
                    'industry': 's.industry COLLATE NOCASE', 'deadline': 's.deadline_sort COLLATE NOCASE',
                    'updated_date': 's.updated_date COLLATE NOCASE', 'priority': 'COALESCE(a.priority,0)',
                    'status': "COALESCE(a.status,'待筛选') COLLATE NOCASE"}
    sort_sql = sort_columns.get(sort, sort_columns['updated_date'])
    direction_sql = 'ASC' if direction.lower() == 'asc' else 'DESC'
    args = [username, base]
    sql = '''SELECT r.table_id,r.record_id,r.payload,r.first_seen,r.last_seen,
                    s.company,s.position,s.industry,s.company_type,s.recruitment_type,s.target,s.location,
                    s.education,s.exam,s.deadline,s.deadline_label,s.deadline_sort,s.updated_date,
                    COALESCE(a.status,'待筛选') AS annotation_status,COALESCE(a.priority,0) AS annotation_priority,
                    COALESCE(a.tags,'[]') AS annotation_tags,COALESCE(a.note,'') AS annotation_note,a.updated_at AS annotation_updated
             FROM feishu_records r JOIN feishu_record_search s
               ON s.base_token=r.base_token AND s.table_id=r.table_id AND s.record_id=r.record_id
             LEFT JOIN feishu_annotations a ON a.username=? AND a.base_token=r.base_token
               AND a.table_id=r.table_id AND a.record_id=r.record_id
             WHERE r.base_token=? AND r.source_missing=0'''
    if table_id:
        sql += ' AND r.table_id=?'; args.append(table_id)
    if q:
        sql += " AND s.search_text LIKE ? ESCAPE '\\'"
        args.append('%' + unicodedata.normalize('NFKC', q[:100]).casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    facet_where = sql[sql.index(' FROM feishu_records r'):]
    facet_args = list(args)
    for column, values in allowed_filters.items():
        choices = [str(value)[:100] for value in values if value][:20]
        if choices:
            sql += ' AND (' + ' OR '.join(f"s.{column} LIKE ? ESCAPE '\\'" for _ in choices) + ')'
            args.extend('%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for value in choices)
    statuses = [value[:40] for value in status if value][:len(FEISHU_STATUSES)]
    if statuses:
        sql += " AND COALESCE(a.status,'待筛选') IN (" + ','.join('?' for _ in statuses) + ')'
        args.extend(statuses)
    focus_levels = [value for value in focus if value in (1, 2, 3)]
    if focus_levels:
        sql += " AND COALESCE(a.status,'待筛选')='关注' AND COALESCE(a.priority,0) IN (" + ','.join('?' for _ in focus_levels) + ')'
        args.extend(focus_levels)
    if priority >= 0:
        sql += ' AND COALESCE(a.priority,0)=?'; args.append(min(priority, 3))
    tags = [value[:40] for value in tag if value][:20]
    if tags:
        sql += " AND EXISTS(SELECT 1 FROM json_each(COALESCE(a.tags,'[]')) WHERE value IN (" + ','.join('?' for _ in tags) + '))'
        args.extend(tags)
    where_part = sql[sql.index(' FROM feishu_records r'):]
    group_columns = {'industry': 's.industry', 'company_type': 's.company_type',
                     'recruitment_type': 's.recruitment_type', 'location': 's.location',
                     'education': 's.education', 'exam': 's.exam',
                     'annotation_status': "COALESCE(a.status,'待筛选')"}
    group_sql = (group_columns[group_by] + ' COLLATE NOCASE, ') if group_by in group_columns else ''
    order_sql = f' ORDER BY {group_sql}{sort_sql} {direction_sql},r.record_id ASC LIMIT ? OFFSET ?'
    with conn() as c:
        total = c.execute('SELECT count(*)' + where_part, args).fetchone()[0]
        rows = [dict(row) for row in c.execute(sql + order_sql, args + [limit, offset]).fetchall()]
        facet_rows = [dict(row) for row in c.execute('''SELECT COALESCE(a.status,'待筛选') AS annotation_status,
            s.industry,s.company_type,s.recruitment_type,s.location,s.education,s.exam,
            COALESCE(a.priority,0) AS annotation_priority,COALESCE(a.tags,'[]') AS annotation_tags''' + facet_where, facet_args).fetchall()]
    facets = {}
    for key in ('annotation_status', 'industry', 'company_type', 'recruitment_type', 'location', 'education', 'exam'):
        facets[key] = {}
        for row in facet_rows:
            values = [row.get(key) or '未填写'] if key == 'annotation_status' else [x.strip() for x in (row.get(key) or '未填写').split('、') if x.strip()]
            for val in set(values):
                facets[key][val] = facets[key].get(val, 0) + 1
        facets[key] = dict(sorted(facets[key].items(), key=lambda pair: -pair[1])[:80])
    facets['tags'] = {}
    for row in facet_rows:
        try: tags = json.loads(row['annotation_tags'])
        except json.JSONDecodeError: tags = []
        for tag_value in set(tags):
            facets['tags'][tag_value] = facets['tags'].get(tag_value, 0) + 1
    facets['tags'] = dict(sorted(facets['tags'].items(), key=lambda pair: -pair[1])[:80])
    facets['focus'] = {str(level): sum(row['annotation_status'] == '关注' and row['annotation_priority'] == level for row in facet_rows)
                       for level in (1, 2, 3)}
    items = []
    for row in rows:
        try: payload = json.loads(row['payload'])
        except json.JSONDecodeError: payload = {'fields': {}}
        try: tags = json.loads(row['annotation_tags'] or '[]')
        except json.JSONDecodeError: tags = []
        items.append({'table_id': row['table_id'], 'record_id': row['record_id'], 'fields': payload.get('fields', payload),
                      'created_time': payload.get('created_time'), 'last_modified_time': payload.get('last_modified_time'),
                      'first_seen': row['first_seen'], 'last_seen': row['last_seen'],
                      'annotation': {'status': row['annotation_status'], 'priority': row['annotation_priority'],
                                     'tags': tags, 'note': row['annotation_note'], 'updated_at': row['annotation_updated']},
                      'deadline': {'raw': row['deadline'], 'label': row['deadline_label'], 'sort': row['deadline_sort']},
                      'group_value': row.get('annotation_status' if group_by == 'annotation_status' else group_by, '') if group_by else ''})
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'facets': facets,
            'sort': sort, 'direction': direction_sql.lower(), 'group_by': group_by, 'status': feishu_status_payload()}


class FeishuAnnotationUpdate(BaseModel):
    status: str = Field(default='待筛选', min_length=1, max_length=40)
    priority: int = Field(default=0, ge=0, le=3)
    tags: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default='', max_length=4000)


FEISHU_STATUSES = {'待筛选', '关注', '已投递', '笔试', '面试', 'Offer', '暂不考虑'}


@app.get('/api/admin/feishu/annotations')
def feishu_annotations(request: Request):
    username = admin(request); base = feishu.target_config()['base_token']
    with conn() as c:
        rows = c.execute('SELECT table_id,record_id,status,priority,tags,note,updated_at FROM feishu_annotations WHERE username=? AND base_token=?', (username, base)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try: item['tags'] = json.loads(item['tags'] or '[]')
        except json.JSONDecodeError: item['tags'] = []
        result.append(item)
    return {'items': result}


@app.put('/api/admin/feishu/annotations/{table_id}/{record_id}')
def update_feishu_annotation(table_id: str, record_id: str, body: FeishuAnnotationUpdate, request: Request):
    username = admin(request); base = feishu.target_config()['base_token']
    if body.status.strip() not in FEISHU_STATUSES:
        raise HTTPException(422, '无效的跟进状态')
    if body.status.strip() == '关注' and body.priority == 0:
        raise HTTPException(422, '请为关注设置一个档位')
    effective_priority = body.priority if body.status.strip() == '关注' else 0
    tags = list(dict.fromkeys(str(tag).strip()[:40] for tag in body.tags if str(tag).strip()))[:20]
    with conn() as c:
        if not c.execute('SELECT 1 FROM feishu_records WHERE base_token=? AND table_id=? AND record_id=? AND source_missing=0', (base, table_id, record_id)).fetchone():
            raise HTTPException(404, '飞书记录不存在')
        c.execute('''INSERT INTO feishu_annotations(username,base_token,table_id,record_id,status,priority,tags,note,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(username,base_token,table_id,record_id) DO UPDATE SET
                     status=excluded.status,priority=excluded.priority,tags=excluded.tags,note=excluded.note,updated_at=excluded.updated_at''',
                  (username, base, table_id, record_id, body.status.strip(), effective_priority, json.dumps(tags, ensure_ascii=False), body.note, now()))
    return {'ok': True, 'status': body.status.strip(), 'priority': effective_priority, 'tags': tags, 'note': body.note}


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
    """Companies with a job posting or a presentation whose scheduled window is not over."""
    records = records if records is not None else effective(True)
    instant = now()
    today = dt.datetime.now(TZ).date().isoformat()

    def event_is_reviewable(item):
        date, end = item.get('date'), item.get('ends_at')
        return not (date and date < today or end and end < instant)

    return {
        item.get('company', '').strip()
        for item in records
        if item.get('company', '').strip()
        and (item.get('kind') == 'job' or item.get('kind') == 'event' and event_is_reviewable(item))
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
