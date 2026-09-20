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
MAX_REVIEW_PENDING = 5
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
        ''')
        review_columns = {row['name'] for row in c.execute('PRAGMA table_info(company_reviews)')}
        if 'usage' not in review_columns:
            c.execute("ALTER TABLE company_reviews ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'")
        c.execute("UPDATE sync_logs SET status='error',finished=?,message='服务重新启动，同步将重试' WHERE status='running'", (now(),))
        c.execute("UPDATE company_reviews SET status='queued',error='服务重新启动，分析将重试' WHERE status='running'")
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


@asynccontextmanager
async def lifespan(app):
    init()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=review_worker, daemon=True).start()
    yield
    stop.set()
    wake.set()
    review_wake.set()


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
        if length > 100000:
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


def effective(include_hidden=False):
    with conn() as c:
        overrides = {r['id']: json.loads(r['data']) for r in c.execute('SELECT * FROM overrides')}
        rows = c.execute('SELECT * FROM records WHERE present=1').fetchall()
        company_reviews = {r['company']: review_dict(r) for r in c.execute('SELECT * FROM company_reviews WHERE score IS NOT NULL AND summary IS NOT NULL')}
    result = []
    today = dt.datetime.now(TZ).date().isoformat()
    instant = now()
    for row in rows:
        item = json.loads(row['data'])
        item.update(overrides.get(row['id'], {}))
        item['modified'] = row['id'] in overrides
        item['review'] = company_reviews.get(item.get('company'))
        if item.get('hidden') and not include_hidden:
            continue
        if item['kind'] == 'job':
            deadline, start = item.get('deadline_date'), item.get('start_date')
            item['status'] = 'expired' if deadline and deadline < today else ('upcoming' if start and start > today else 'open' if deadline else 'unknown')
        else:
            date, start, end = item.get('date'), item.get('starts_at'), item.get('ends_at')
            item['status'] = 'unknown' if not date else ('ended' if date < today or end and end < instant else 'today' if date == today else 'upcoming')
            if date == today and item.get('time_known') and start and start < instant and not end:
                item['status'] = 'started'
        result.append(item)
    return result


@app.get('/api/health')
def health():
    with conn() as c:
        c.execute('SELECT 1').fetchone()
    return {'ok': True}


@app.get('/api/public')
def public():
    cfg = settings()
    return {'records': effective(), 'config': {k: cfg[k] for k in DEFAULTS},
            'sync': {'last_success': cfg.get('last_success'),
                     'next_sync': dt.datetime.fromtimestamp(cfg['next_sync'], TZ).isoformat() if cfg['auto_sync'] else None,
                     'has_error': bool(cfg.get('last_error')), 'running': sync_lock.locked(), 'interval_minutes': 15,
                     'source_title': cfg.get('source_meta', {}).get('title'),
                     'source_version': cfg.get('source_meta', {}).get('version')}, 'server_time': now()}


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
    records = effective(True)
    companies = sorted(reviewable_companies(records))
    reviews = [stored_reviews.get(company) or {'company': company, 'status': 'unreviewed', 'score': None,
                                               'label': None, 'summary': None, 'pros': [], 'cons': [],
                                               'sources': [], 'provider': None, 'updated_at': None,
                                               'queued_at': None, 'error': None}
               for company in companies]
    return {'username': username, 'records': records, 'config': {k: cfg[k] for k in DEFAULTS},
            'logs': logs, 'audit': audits, 'last_error': cfg.get('last_error'), 'sync_running': sync_lock.locked(),
            'reviews': reviews, 'review_config': review.config(), 'review_running': review_lock.locked(),
            'pushplus': pushplus_status(cfg)}


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
        and (item.get('kind') == 'job' or item.get('status') != 'ended')
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
           'apply_url', 'announcement_url', 'notes', 'updated', 'deadline', 'start', 'time_text', 'hidden', 'pinned'}


def validated_patch(patch, kind):
    if set(patch) - ALLOWED:
        raise HTTPException(422, '包含不允许修改的字段')
    for k, v in patch.items():
        if k in ('hidden', 'pinned'):
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
            raise HTTPException(422, '宣讲会时间请包含完整日期')
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


@app.post('/api/admin/records')
def add_record(body: dict, request: Request):
    admin(request)
    kind = body.pop('kind', None)
    if kind not in ('job', 'event'):
        raise HTTPException(422, '类型不正确')
    if not body.get('company') or kind == 'event' and not body.get('time_text'):
        raise HTTPException(422, '请填写单位名称和必要的宣讲会时间')
    patch = validated_patch(body, kind)
    rid = 'manual-' + uuid.uuid4().hex
    record = {'id': rid, 'kind': kind, 'source': 'manual', 'source_sheet': '人工补充', 'source_row': None,
              'hidden': False, 'pinned': False, 'updated': dt.datetime.now(TZ).date().isoformat(), **patch}
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
