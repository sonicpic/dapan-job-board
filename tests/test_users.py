import datetime as dt
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_app import client, sign_in, sync_fixture
import app
import feishu


def second_client():
    return TestClient(app.app, base_url='https://jobs.example.com',
                      headers={'X-Requested-With': 'job-board'})


def invite(admin_client, uses=2, days=7):
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()
    response = admin_client.post('/api/admin/invites', json={'max_uses': uses, 'expires_at': expiry})
    assert response.status_code == 200, response.text
    return response.json()['code']


def register(user_client, username, code, password='StrongPass123!'):
    return user_client.post('/api/register', json={'username': username, 'password': password,
                                                  'confirm': password, 'invite_code': code})


def test_invite_registration_expiry_use_limit_and_password_rules(client):
    assert register(client, 'alice', 'invalid-code').status_code == 400
    sign_in(client)
    code = invite(client, uses=1)
    listed = client.get('/api/admin/invites').json()['items']
    assert len(listed) == 1 and code not in json.dumps(listed)
    newcomer = second_client()
    assert register(newcomer, 'alice', code, 'weak-password').status_code == 422
    assert register(newcomer, 'alice', code).status_code == 200
    assert register(newcomer, 'bob', code).status_code == 400
    assert newcomer.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).json()['role'] == 'user'
    assert newcomer.get('/api/session').json() == {'admin': False, 'role': 'user', 'username': 'alice'}
    assert client.delete('/api/admin/invites/' + listed[0]['code_hash']).status_code == 200
    assert client.get('/api/admin/invites').json()['items'] == []


def test_users_share_public_records_but_not_private_state_or_admin_access(client):
    sync_fixture()
    with app.conn() as c:
        c.execute('INSERT INTO records VALUES(?,?,?,?,1)', ('private-job', 'job', 'manual', json.dumps({
            'id': 'private-job', 'kind': 'job', 'company': '内部职位', 'visitor_visible': False,
            'archived': False, 'source': 'manual'})))
    sign_in(client)
    code = invite(client, uses=2)
    alice, bob = second_client(), second_client()
    assert register(alice, 'alice', code).status_code == 200
    assert register(bob, 'bob', code).status_code == 200
    for user, name in ((alice, 'alice'), (bob, 'bob')):
        assert user.post('/api/login', json={'username': name, 'password': 'StrongPass123!'}).status_code == 200
        assert user.get('/api/admin').status_code == 403
        assert user.get('/api/admin/feishu/records').status_code == 403
        assert user.post('/api/admin/feishu/sync').status_code == 403
        assert user.get('/api/admin/bookmarks').status_code == 403
        assert 'private-job' not in [row['id'] for row in user.get('/api/public').json()['records']]
        assert user.put('/api/me/bookmarks/private-job').status_code == 404
    assert alice.put('/api/me/bookmarks/source-job-1').status_code == 200
    assert alice.get('/api/me/bookmarks').json()['ids'] == ['source-job-1']
    assert bob.get('/api/me/bookmarks').json()['ids'] == []

    snapshot = {'base_token': 'app-test', 'tables': [{
        'table': {'table_id': 'tbl1', 'name': '职位'}, 'fields': [], 'views': [],
        'records': [{'record_id': 'rec1', 'fields': {'公司': '测试公司'}}],
    }]}
    with patch.object(feishu, 'snapshot', return_value=snapshot), patch.object(feishu, 'target_config',
            return_value={'base_token': 'app-test', 'url': '', 'table_id': 'tbl1', 'view_id': ''}):
        assert app.run_feishu_sync()
        assert alice.get('/api/workspace/records').json()['total'] == 1
        assert bob.get('/api/workspace/records').json()['total'] == 1
        body = {'status': '关注', 'priority': 3, 'tags': ['必投'], 'note': 'Alice 的个人备注'}
        assert alice.put('/api/workspace/annotations/tbl1/rec1', json=body).status_code == 200
        alice_row = alice.get('/api/workspace/records').json()['items'][0]
        bob_row = bob.get('/api/workspace/records').json()['items'][0]
        assert alice_row['annotation']['note'] == 'Alice 的个人备注'
        assert bob_row['annotation']['note'] == ''
        assert client.get('/api/workspace/records').json()['items'][0]['annotation']['note'] == ''
    assert second_client().get('/api/workspace/records').status_code == 401


def test_normal_user_can_change_only_own_password(client):
    sign_in(client)
    code = invite(client, uses=1)
    user = second_client()
    assert register(user, 'alice', code).status_code == 200
    assert user.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).status_code == 200
    assert user.put('/api/admin/password', json={'current': 'StrongPass123!', 'password': 'NewStrongPass123!'}).status_code == 403
    assert user.put('/api/me/password', json={'current': 'wrong', 'password': 'NewStrongPass123!'}).status_code == 400
    assert user.put('/api/me/password', json={'current': 'StrongPass123!', 'password': 'NewStrongPass123!'}).status_code == 200
    assert user.get('/api/workspace/records').status_code == 401
    assert user.post('/api/login', json={'username': 'alice', 'password': 'NewStrongPass123!'}).status_code == 200
    assert client.get('/api/admin').status_code == 200


def test_legacy_roles_migrate_and_new_accounts_default_to_user(client):
    with app.conn() as c:
        c.execute('DROP TABLE users')
        c.execute('CREATE TABLE users(username TEXT PRIMARY KEY,password TEXT NOT NULL)')
        c.execute('INSERT INTO users VALUES(?,?)', ('legacy', app.hash_pw('LegacyPass123!')))
    app.init()
    with app.conn() as c:
        assert c.execute("SELECT role FROM users WHERE username='legacy'").fetchone()[0] == 'admin'
        assert next(row for row in c.execute('PRAGMA table_info(users)') if row['name'] == 'role')['dflt_value'] == "'user'"
        assert c.execute("SELECT enabled FROM users WHERE username='legacy'").fetchone()[0] == 1
        c.execute('DROP TABLE users')
        c.execute("CREATE TABLE users(username TEXT PRIMARY KEY,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'admin')")
        c.execute('INSERT INTO users VALUES(?,?,?)', ('legacy', app.hash_pw('LegacyPass123!'), 'admin'))
    app.init()
    with app.conn() as c:
        assert c.execute("SELECT role FROM users WHERE username='legacy'").fetchone()[0] == 'admin'
        assert next(row for row in c.execute('PRAGMA table_info(users)') if row['name'] == 'role')['dflt_value'] == "'user'"
        assert c.execute("SELECT enabled FROM users WHERE username='legacy'").fetchone()[0] == 1


def test_admin_user_lifecycle_revokes_sessions_and_deletes_private_data(client):
    sign_in(client)
    user = second_client()
    assert register(user, 'alice', invite(client, uses=1)).status_code == 200
    assert user.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).status_code == 200
    with app.conn() as c:
        c.execute('INSERT INTO admin_bookmarks VALUES(?,?,?)', ('alice', 'sample', app.now()))
        c.execute('''INSERT INTO feishu_annotations(username,base_token,table_id,record_id,status,priority,tags,note,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)''', ('alice', 'base', 'table', 'record', '关注', 3, '[]', '私有备注', app.now()))
    assert user.get('/api/admin/users').status_code == 403
    assert client.put('/api/admin/users/admin/enabled', json={'enabled': False}).status_code == 403
    assert client.post('/api/admin/users/admin/reset-password').status_code == 403
    assert client.delete('/api/admin/users/admin').status_code == 403
    listing = client.get('/api/admin/users').json()['items']
    assert listing == [{'username': 'alice', 'role': 'user', 'enabled': True, 'bookmarks': 1, 'annotations': 1}]
    assert client.put('/api/admin/users/alice/enabled', json={'enabled': False}).status_code == 200
    assert user.get('/api/session').json()['role'] == 'guest'
    assert user.get('/api/workspace/records').status_code == 401
    assert user.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).status_code == 401
    assert client.put('/api/admin/users/alice/enabled', json={'enabled': True}).status_code == 200
    assert user.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).status_code == 200
    password = client.post('/api/admin/users/alice/reset-password').json()['password']
    assert app.strong_password(password)
    assert user.get('/api/session').json()['role'] == 'guest'
    assert user.get('/api/workspace/records').status_code == 401
    assert user.post('/api/login', json={'username': 'alice', 'password': 'StrongPass123!'}).status_code == 401
    assert user.post('/api/login', json={'username': 'alice', 'password': password}).status_code == 200
    assert client.delete('/api/admin/users/alice').status_code == 200
    assert user.get('/api/session').json()['role'] == 'guest'
    assert user.get('/api/workspace/records').status_code == 401
    assert user.post('/api/login', json={'username': 'alice', 'password': password}).status_code == 401
    with app.conn() as c:
        assert c.execute("SELECT count(*) FROM admin_bookmarks WHERE username='alice'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM feishu_annotations WHERE username='alice'").fetchone()[0] == 0


def test_feishu_placeholders_are_excluded_from_all_workspace_counts(client):
    sign_in(client)
    snapshot = {'base_token': 'app-test', 'tables': [{
        'table': {'table_id': app.FEISHU_PLACEHOLDER_TABLE, 'name': '职位'}, 'fields': [], 'views': [],
        'records': [
            {'record_id': app.FEISHU_PLACEHOLDER_RECORDS[0], 'fields': {'公司名称': '27届【秋招/春招/实习】汇总表  4'}},
            {'record_id': app.FEISHU_PLACEHOLDER_RECORDS[1], 'fields': {'网申更新': '2099-01-01 00:00'}},
            {'record_id': 'real-job', 'fields': {'公司名称': '真实公司', '工作地点': '北京'}},
        ],
    }]}
    with patch.object(feishu, 'snapshot', return_value=snapshot), patch.object(feishu, 'target_config',
            return_value={'base_token': 'app-test', 'url': '', 'table_id': app.FEISHU_PLACEHOLDER_TABLE, 'view_id': ''}):
        assert app.run_feishu_sync()
        page = client.get('/api/workspace/records').json()
        assert page['total'] == 1
        assert [item['record_id'] for item in page['items']] == ['real-job']
        assert client.get('/api/workspace/manifest').json()['status']['records'] == 1
        assert client.get('/api/workspace/manifest').json()['tables'][0]['records'] == 1
    with app.conn() as c:
        assert c.execute('SELECT count(*) FROM feishu_records').fetchone()[0] == 3
