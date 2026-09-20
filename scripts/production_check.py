"""Run on the server. Verify production auth without printing credentials."""
import http.cookiejar
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path('/opt/job-board')
BASE = os.getenv('CHECK_BASE', 'http://localhost:8000').rstrip('/')
credentials = (ROOT / 'data/initial-admin.txt').read_text()
password = next(line.split('：', 1)[1] for line in credentials.splitlines() if line.startswith('初始密码：'))
jar = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, method='GET', body=None):
    request = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'Origin': BASE, 'X-Requested-With': 'job-board',
                 'User-Agent': 'Mozilla/5.0 (compatible; JobBoardHealthCheck/1.0)'})
    with client.open(request, timeout=30) as response:
        return json.load(response)


assert call('/api/login', 'POST', {'username': 'admin', 'password': password})['username'] == 'admin'
try:
    dashboard = call('/api/admin')
    assert len(dashboard['records']) >= 80
    print('Last synchronization error:', dashboard.get('last_error'))
    assert call('/api/admin/settings', 'PUT', dashboard['config'])['ok']
    assert call('/api/admin')['config'] == dashboard['config']
    if os.getenv('TRIGGER_SYNC') == '1':
        assert call('/api/admin/sync', 'POST')['ok']
        print('Manual synchronization started')
    print('Production login, authenticated dashboard and settings save: PASS')
finally:
    assert call('/api/logout', 'POST')['ok']
    try:
        call('/api/admin')
        raise AssertionError('Logout left the health-check session active')
    except urllib.error.HTTPError as error:
        assert error.code == 401

connection = sqlite3.connect(ROOT / 'data/jobs.sqlite3')
connection.row_factory = sqlite3.Row
print('Recent sync logs:', json.dumps([dict(row) for row in connection.execute(
    'SELECT started,finished,status,jobs,events,changed FROM sync_logs ORDER BY id DESC LIMIT 3')]))
print('Logout invalidated session: PASS')
