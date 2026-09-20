import copy
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='job-board-tests-')
os.environ['ADMIN_PASSWORD'] = 'test-only-long-password'
from fastapi.testclient import TestClient
import app
from source import normalize_date, safe_link, parse_sheet
import source
import recording

@pytest.fixture
def client(tmp_path):
    app.DB = tmp_path / 'test.sqlite3'
    app.RECORDINGS = tmp_path / 'recordings'
    app.RECORDINGS.mkdir()
    app.init()
    return TestClient(app.app, base_url='https://jobs.example.com', headers={'X-Requested-With': 'job-board'})


def sign_in(client):
    response = client.post('/api/login', json={'username': 'admin', 'password': os.environ['ADMIN_PASSWORD']})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie


def test_unknown_user_cannot_authenticate_with_dummy_password(client):
    response = client.post('/api/login', json={'username': 'unknown-user', 'password': 'dummy-password'})
    assert response.status_code == 401
    assert 'job_session' not in response.cookies
    assert client.get('/api/admin').status_code == 401


def fixture_items():
    return [dict(id='source-job-1', kind='job', company='测试公司', source='kdocs', source_row=3, hidden=False, pinned=False,
                 positions='软件工程师', apply_url='https://example.com/jobs', deadline_date='2026-09-30'),
            dict(id='source-event-1', kind='event', company='测试宣讲会', source='kdocs', source_row=2, hidden=False, pinned=False,
                 starts_at='2026-09-21T14:00:00+08:00', date='2026-09-21', time_known=True)]


def sync_fixture(items=None):
    with patch.object(app, 'fetch_source', return_value=(items or fixture_items(), {'title':'测试表', 'version':1})):
        app.run_sync()


@pytest.mark.parametrize('value,expected', [('2026.9.15','2026-09-15'), (46295.0,'2026-09-30'),
    ('2026-09-16（星期三）16:00 - 17:30','2026-09-16'), ('9月21日','2026-09-21'), (9.14,'2026-09-14'),
    ('待定',None),('2026-02-31',None)])
def test_dates(value, expected):
    assert normalize_date(value, 2026) == expected


def test_event_times():
    assert normalize_date(46280.375,2026,True) == '2026-09-15T09:00:00+08:00'
    assert normalize_date('9月16日晚上7:00-8:00',2026,True) == '2026-09-16T19:00:00+08:00'
    assert normalize_date('09-1414:00',2026,True) == '2026-09-14T14:00:00+08:00'


def test_header_detection_tolerates_renames_reordering_and_notice_rows():
    job_rows = {
        0: {0: '请同学们及时查看最新信息'},
        3: {0: '公司名称 ', 1: '招聘职位（方向）', 2: '投递截止日期', 3: '申请链接', 4: '更新日期'},
        4: {0: '示例科技', 1: '后端工程师', 2: '2026-10-01', 3: 'https://example.com/apply', 4: '2026-09-20'},
    }
    event_rows = {
        2: {0: '企业名称', 1: '活动日期及时间', 2: '举办地点', 3: '补充信息'},
        3: {0: '示例集团', 1: '2026-09-25 14:00', 2: '教学楼', 3: '现场宣讲'},
    }
    with patch.object(source, 'extract', return_value=(job_rows, {})):
        jobs = source.parse_sheet({}, '招聘信息', 2026)
    with patch.object(source, 'extract', return_value=(event_rows, {})):
        events = source.parse_sheet({}, '宣讲会信息', 2026)
    assert jobs[0]['company'] == '示例科技'
    assert jobs[0]['positions'] == '后端工程师'
    assert jobs[0]['deadline_date'] == '2026-10-01'
    assert jobs[0]['apply_url'] == 'https://example.com/apply'
    assert events[0]['company'] == '示例集团'
    assert events[0]['starts_at'] == '2026-09-25T14:00:00+08:00'
    assert events[0]['location'] == '教学楼'


def test_links():
    assert safe_link('campus.glodon.com') == 'https://campus.glodon.com'
    assert safe_link('联系人：某某\n邮箱：person@example.com') == 'mailto:person@example.com'
    assert safe_link('javascript:alert(1)') == ''
    assert safe_link('=DISPIMG("id",1)') == ''


def test_auth_and_origin(client):
    assert client.get('/api/session').json() == {'admin': False}
    assert client.get('/api/admin').status_code == 401
    assert client.post('/api/admin/sync').status_code == 401
    assert client.post('/api/login',json={'username':'admin','password':'incorrect'}).status_code == 401
    assert client.post('/api/login',headers={'Origin':'https://evil.example'},json={'username':'admin','password':'test-only-long-password'}).status_code == 403
    sign_in(client)
    assert client.get('/api/session').json()['admin'] is True
    assert client.get('/api/admin').status_code == 200
    client.post('/api/logout')
    assert client.get('/api/admin').status_code == 401


def test_admin_bookmarks_sync_through_account(client):
    sync_fixture(); sign_in(client)
    assert client.get('/api/admin/bookmarks').json() == {'ids': []}
    assert client.put('/api/admin/bookmarks/source-job-1').status_code == 200
    assert client.put('/api/admin/bookmarks/source-event-1').status_code == 200
    assert client.put('/api/admin/bookmarks/missing-record').status_code == 404
    assert set(client.get('/api/admin/bookmarks').json()['ids']) == {'source-job-1', 'source-event-1'}
    assert client.delete('/api/admin/bookmarks/source-job-1').status_code == 200
    assert client.get('/api/admin/bookmarks').json()['ids'] == ['source-event-1']


def test_recording_upload_process_visibility_and_delete(client, monkeypatch):
    sync_fixture(); sign_in(client)
    path = '/api/admin/events/source-event-1/recording'
    uploaded = client.post(path, content=b'fake-m4a-audio', headers={
        'Content-Type': 'audio/mp4', 'X-File-Name': 'campus-talk.m4a',
    })
    assert uploaded.status_code == 200
    assert uploaded.json()['status'] == 'uploaded'
    assert client.get(path).json()['transcript'] is None
    public_event = next(item for item in client.get('/api/public').json()['records'] if item['kind'] == 'event')
    assert 'recording' not in public_event and 'recording_summary' not in public_event

    monkeypatch.setenv('ASR_DASHSCOPE_API_KEY', 'test-asr-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-summary-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://model.example/v1')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-5.6-luna')
    assert client.post(path + '/process').status_code == 200
    with patch.object(recording, 'submit_asr', return_value='task-123') as submit, \
         patch.object(recording, 'wait_asr', return_value={'task_status': 'SUCCEEDED'}), \
         patch.object(recording, 'fetch_transcript', return_value='这是完整原始转写。'), \
         patch.object(recording, 'summarize', return_value='## 核心信息\n这是管理员确认后的完整总结。'):
        assert app.process_recording_queue() is True
    assert submit.call_args.args[0].startswith('http')
    detail = client.get(path).json()
    assert detail['status'] == 'completed'
    assert detail['transcript'] == '这是完整原始转写。'
    assert detail['summary_public'] is False
    assert client.put(path + '/visibility', json={'public': True}).status_code == 200
    public_event = next(item for item in client.get('/api/public').json()['records'] if item['kind'] == 'event')
    assert public_event['recording_summary'].startswith('## 核心信息')
    assert 'transcript' not in json.dumps(public_event, ensure_ascii=False)
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404


def test_recording_permissions_and_file_validation(client):
    sync_fixture()
    path = '/api/admin/events/source-event-1/recording'
    assert client.post(path, content=b'audio', headers={'X-File-Name': 'talk.m4a'}).status_code == 401
    sign_in(client)
    assert client.post(path, content=b'audio', headers={'X-File-Name': '../attack.exe'}).status_code == 422
    assert client.post('/api/admin/events/source-job-1/recording', content=b'audio',
                       headers={'X-File-Name': 'talk.m4a'}).status_code == 404


def test_qwen_filetrans_payload_and_transcript_result(monkeypatch):
    monkeypatch.setenv('ASR_DASHSCOPE_API_KEY', 'test-key')
    calls = []
    with patch.object(recording, '_json_request', side_effect=lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {'output': {'task_id': 'task-1'}}
    )):
        assert recording.submit_asr('http://jobs.example.com/audio-token') == 'task-1'
    assert calls[0][0][0].endswith('/services/audio/asr/transcription')
    assert calls[0][0][1]['model'] == 'qwen-audio-3.0-asr-flash-filetrans'
    assert calls[0][0][1]['input']['file_url'] == 'http://jobs.example.com/audio-token'
    assert calls[0][0][2]['X-DashScope-Async'] == 'enable'
    result = {'results': [{'transcription_url': 'https://result.example/transcript.json'}]}
    with patch.object(recording, '_json_request', return_value={
        'transcripts': [{'text': '第一部分。'}, {'text': '第二部分。'}]
    }):
        assert recording.fetch_transcript(result) == '第一部分。\n\n第二部分。'


def test_http_ip_login_uses_non_secure_cookie(tmp_path, monkeypatch):
    app.DB = tmp_path / 'ip-test.sqlite3'
    app.init()
    monkeypatch.setattr(app, 'ORIGINS', app.ORIGINS | {'http://203.0.113.10'})
    ip_client = TestClient(app.app, base_url='http://203.0.113.10',
                           headers={'X-Requested-With': 'job-board', 'Origin': 'http://203.0.113.10'})
    response = ip_client.post('/api/login', json={'username': 'admin', 'password': os.environ['ADMIN_PASSWORD']})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Secure' not in cookie
    assert ip_client.get('/api/admin').status_code == 200


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/login',json={'username':'admin','password':'wrong'}).status_code == 401
    assert client.post('/api/login',json={'username':'admin','password':'wrong'}).status_code == 429


def test_sync_failure_preserves_records_and_success(client):
    sync_fixture()
    previous=client.get('/api/public').json()
    with patch.object(app,'fetch_source',side_effect=ValueError('源表暂时不可用')):
        app.run_sync()
    current=client.get('/api/public').json()
    assert len(current['records']) == 2
    assert current['sync']['last_success'] == previous['sync']['last_success']
    assert current['sync']['has_error']
    assert app.settings()['next_sync'] > __import__('time').time()+890
    sync_fixture()
    assert not client.get('/api/public').json()['sync']['has_error']


def test_source_drop_preserves_previous_records_without_sync_error(client):
    many = [dict(fixture_items()[0], id=f'job-{index}', company=f'测试公司{index}') for index in range(20)]
    sync_fixture(many)
    sync_fixture(many[:5])
    public = client.get('/api/public').json()
    assert len(public['records']) == 20
    assert not public['sync']['has_error']


def test_overrides_survive_sync_and_changed_link(client):
    sync_fixture();sign_in(client)
    assert client.patch('/api/admin/records/source-job-1',json={'positions':'管理员补充','pinned':True,'hidden':True}).status_code == 200
    assert len(client.get('/api/public').json()['records']) == 1
    changed=fixture_items();changed[0].update(id='new-source-job-id',apply_url='https://example.com/new-jobs',positions='上游新内容')
    sync_fixture(changed)
    records=client.get('/api/admin').json()['records']
    job=next(r for r in records if r['kind']=='job')
    assert job['id']=='source-job-1' and job['positions']=='管理员补充' and job['hidden'] and job['pinned']
    assert job['apply_url']=='https://example.com/new-jobs'
    client.delete('/api/admin/records/source-job-1/override')
    assert len(client.get('/api/public').json()['records']) == 2
    assert next(r for r in client.get('/api/public').json()['records'] if r['kind']=='job')['positions']=='上游新内容'


def test_manual_and_source_history_survives_upstream_removal(client):
    sign_in(client);sync_fixture()
    result=client.post('/api/admin/records',json={'kind':'job','company':'手动新增','positions':'测试职位'})
    assert result.status_code==200
    sync_fixture([fixture_items()[1]])
    records=client.get('/api/public').json()['records']
    assert len(records)==3 and any(r['source']=='manual' for r in records)
    assert any(r['id']=='source-job-1' for r in records)


def test_source_sync_deduplicates_logically_identical_rows(client):
    duplicate = dict(fixture_items()[0], id='different-upstream-id')
    sync_fixture([fixture_items()[0], duplicate])
    jobs = [r for r in client.get('/api/public').json()['records'] if r['kind'] == 'job']
    assert len(jobs) == 1


def test_sync_pushes_only_new_events_after_initial_import(client, monkeypatch):
    monkeypatch.setenv('PUSHPLUS_TOKEN', 'test-token')
    new_event = dict(fixture_items()[1], id='source-event-2', company='新增宣讲企业',
                     starts_at='2026-09-25T19:00:00+08:00', date='2026-09-25',
                     time_text='2026-09-25 19:00')
    with patch.object(app, 'send_pushplus', return_value=True) as sender:
        sync_fixture()
        sender.assert_not_called()
        sync_fixture(fixture_items() + [new_event])
    sender.assert_called_once()
    assert [(item['company'], item['time_text']) for item in sender.call_args.args[0]] == [
        ('新增宣讲企业', '2026-09-25 19:00')
    ]


def test_pushplus_admin_management_and_payload(client, monkeypatch):
    sign_in(client)
    monkeypatch.setenv('PUSHPLUS_TOKEN', 'test-token')
    assert client.put('/api/admin/pushplus', json={'enabled': False}).status_code == 200
    assert client.get('/api/admin').json()['pushplus']['enabled'] is False
    assert client.put('/api/admin/pushplus', json={'enabled': True}).status_code == 200
    with patch.object(app, 'send_pushplus', return_value=True) as sender:
        assert client.post('/api/admin/pushplus/test').status_code == 200
    sender.assert_called_once_with([{'company': '测试企业', 'time_text': sender.call_args.args[0][0]['time_text']}], test=True)


def test_pushplus_payload_only_contains_company_and_time(monkeypatch):
    monkeypatch.setenv('PUSHPLUS_TOKEN', 'test-token')
    monkeypatch.setenv('PUSHPLUS_BASE_URL', '')
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"code":200}'
    def open_request(request, timeout=15):
        captured.update(json.loads(request.data.decode('utf-8')))
        return Response()
    with patch.object(app.urllib.request, 'urlopen', side_effect=open_request):
        app.send_pushplus([{'company':'示例企业','time_text':'2026-09-25 14:00','location':'不应推送'}])
    assert captured['content'] == '企业：示例企业\n时间：2026-09-25 14:00'
    assert '不应推送' not in captured['content']


def test_record_validation(client):
    sign_in(client);sync_fixture()
    for patch_body in [{'apply_url':'javascript:alert(1)'},{'hidden':'false'},{'id':'hacked'},{'company':''},{'deadline':'garbage'}]:
        assert client.patch('/api/admin/records/source-job-1',json=patch_body).status_code==422


def test_password_revokes_sessions(client):
    sign_in(client)
    assert client.put('/api/admin/password',json={'current':'wrong','password':'new-secure-password'}).status_code==400
    assert client.put('/api/admin/password',json={'current':os.environ['ADMIN_PASSWORD'],'password':'new-secure-password'}).status_code==200
    assert client.get('/api/admin').status_code==401


def test_review_is_disabled_without_provider(client, monkeypatch):
    sync_fixture();sign_in(client)
    monkeypatch.delenv('REVIEW_MODE', raising=False)
    with patch.object(app.review, 'analyze', side_effect=AssertionError('disabled mode must not call a provider')):
        response = client.post('/api/admin/reviews/analyze', json={'company':'测试公司'})
        assert response.status_code == 409
        assert app.process_review_queue() is False
    dashboard = client.get('/api/admin').json()
    row = next(item for item in dashboard['reviews'] if item['company']=='测试公司')
    assert row['status'] == 'unreviewed' and row['score'] is None
    assert dashboard['review_config']['configured'] is False


def test_review_queue_publishes_verified_result(client, monkeypatch):
    sync_fixture();sign_in(client)
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    fake = ({'score':78, 'label':'较推荐', 'summary':'公开讨论中，培养机会评价较多，但不同团队体验存在差异。',
             'pros':['培养体系较完整'], 'cons':['部分团队工作强度较高'],
             'sources':[{'title':'公开讨论页', 'url':'https://example.com/review', 'excerpt':''}]},
            {'name':'测试搜索服务', 'model':'test-model'})
    assert client.post('/api/admin/reviews/analyze', json={'company':'测试公司'}).status_code == 200
    with patch.object(app.review, 'analyze', return_value=fake) as analyze:
        assert app.process_review_queue() is True
        analyze.assert_called_once_with('测试公司')
    public = client.get('/api/public').json()
    item = next(record for record in public['records'] if record['company']=='测试公司')
    assert item['review']['score'] == 78
    assert item['review']['label'] == '较推荐'
    assert item['review']['sources'][0]['url'] == 'https://example.com/review'
    assert 'error' not in item['review'] and 'provider' not in item['review']
    dashboard = client.get('/api/admin').json()
    row = next(item for item in dashboard['reviews'] if item['company']=='测试公司')
    assert row['status'] == 'success' and row['provider'] == '测试搜索服务 / test-model'


def test_review_batch_only_queues_missing_companies(client, monkeypatch):
    companies = [dict(fixture_items()[0], id=f'job-{index}', company=f'测试公司{index}') for index in range(8)]
    sync_fixture(companies);sign_in(client)
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    first = client.post('/api/admin/reviews/batch', json={'mode':'missing','max_items':5})
    assert first.status_code == 200 and first.json()['queued'] == 5
    second = client.post('/api/admin/reviews/batch', json={'mode':'missing','max_items':5})
    assert second.status_code == 200 and second.json()['queued'] == 0


def test_ended_event_only_company_is_not_reviewable(client, monkeypatch):
    ended = dict(fixture_items()[1], id='ended-event', company='往期宣讲企业',
                 starts_at='2000-01-01T09:00:00+08:00', date='2000-01-01')
    sync_fixture([fixture_items()[0], ended]); sign_in(client)
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    companies = [row['company'] for row in client.get('/api/admin').json()['reviews']]
    assert '测试公司' in companies
    assert '往期宣讲企业' not in companies
    assert client.post('/api/admin/reviews/analyze', json={'company': '往期宣讲企业'}).status_code == 404


def test_openai_extracts_cli_proxy_open_page_url(monkeypatch):
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-openai-key')

    def fake_post(*args, **kwargs):
        return {
            'output': [
                {'type': 'web_search_call', 'action': {'type': 'search', 'query': '测试公司 网评'}},
                {'type': 'web_search_call', 'action': {'type': 'open_page', 'url': 'https://example.com/opened-page'}},
                {'type': 'message', 'content': [{'type': 'output_text', 'annotations': [], 'text': json.dumps({
                    'score': 66, 'summary': '公开讨论呈现出机会和顾虑。',
                    'pros': ['成长机会'], 'cons': ['节奏较快'],
                }, ensure_ascii=False)}]},
            ],
            'usage': {'input_tokens': 12000, 'output_tokens': 300, 'total_tokens': 12300},
        }

    with patch.object(app.review, '_post_json', side_effect=fake_post):
        result, _ = app.review.analyze('测试公司')

    assert result['sources'][0]['url'] == 'https://example.com/opened-page'
    assert result['usage']['search_count'] == 2


def test_openai_extracts_sources_from_structured_result(monkeypatch):
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-openai-key')

    def fake_post(*args, **kwargs):
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
            'score': 70, 'summary': '综合公开材料后的总结。', 'pros': ['发展机会'], 'cons': ['节奏较快'],
            'sources': [
                {'title': '讨论一', 'url': 'https://example.com/one'},
                {'title': '讨论二', 'url': 'https://example.org/two'},
                {'title': '讨论三', 'url': 'https://example.net/three'},
            ],
        }, ensure_ascii=False)}]}], 'usage': {'total_tokens': 1000}}

    with patch.object(app.review, '_post_json', side_effect=fake_post):
        result, _ = app.review.analyze('测试公司')

    assert [source['url'] for source in result['sources']] == [
        'https://example.com/one', 'https://example.org/two', 'https://example.net/three'
    ]


def test_review_failure_saves_usage_and_continues_queue(client, monkeypatch):
    companies = [dict(fixture_items()[0], id=f'job-{index}', company=f'测试公司{index}') for index in range(3)]
    sync_fixture(companies);sign_in(client)
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    assert client.post('/api/admin/reviews/batch', json={'mode':'missing','max_items':3}).json()['queued'] == 3
    failure = app.review.ProviderResponseError('没有来源', {'total_tokens': 12345, 'input_tokens': 12000})
    with patch.object(app.review, 'analyze', side_effect=failure):
        assert app.process_review_queue() is True
    rows = client.get('/api/admin').json()['reviews']
    assert sum(row['status'] == 'error' for row in rows) == 1
    assert sum(row['status'] == 'queued' for row in rows) == 2
    failed = next(row for row in rows if row['error'] == '没有来源')
    assert failed['usage']['total_tokens'] == 12345


def test_openai_responses_web_search_records_usage(monkeypatch):
    monkeypatch.setenv('REVIEW_MODE', 'openai-web-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-openai-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://proxy.example/v1')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-5.6-luna')
    monkeypatch.setenv('OPENAI_PROVIDER_NAME', 'CLIProxyAPI 联网搜索')
    calls = []

    def fake_post(url, payload, headers=None, timeout=90):
        calls.append((url, payload, headers))
        return {
            'output': [
                {'type': 'web_search_call', 'action': {'sources': [
                    {'title': '员工公开讨论', 'url': 'https://example.com/source'},
                ]}},
                {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                    'score': 70,
                    'summary': '公开讨论同时提到成长机会和工作节奏。',
                    'pros': ['有成长机会'],
                    'cons': ['工作节奏较快'],
                }, ensure_ascii=False)}]},
            ],
            'usage': {'input_tokens': 8000, 'output_tokens': 300, 'total_tokens': 8300},
        }

    with patch.object(app.review, '_post_json', side_effect=fake_post):
        result, provider = app.review.analyze('测试公司')

    assert provider['name'] == 'CLIProxyAPI 联网搜索'
    assert result['usage'] == {
        'input_tokens': 8000,
        'output_tokens': 300,
        'total_tokens': 8300,
        'search_count': 1,
    }
    assert calls[0][0] == 'https://proxy.example/v1/responses'
    assert calls[0][1]['model'] == 'gpt-5.6-luna'
    assert calls[0][1]['reasoning'] == {'effort': 'low'}
    assert calls[0][1]['max_output_tokens'] == 1200
    assert calls[0][2]['Authorization'] == 'Bearer test-openai-key'
    assert calls[0][2]['User-Agent'].startswith('Mozilla/5.0')


def test_bailian_responses_uses_builtin_web_sources(monkeypatch):
    monkeypatch.setenv('REVIEW_MODE', 'bailian-web-search')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-bailian-key')
    calls = []

    def fake_post(url, payload, headers=None, timeout=90):
        calls.append((url, payload, headers))
        return {'output': [
            {'type':'web_search_call', 'action':{'sources':[{'title':'员工公开讨论', 'url':'https://example.com/source'}]}},
            {'type':'message', 'content':[{'type':'output_text', 'text':json.dumps({
                'score':68, 'summary':'公开讨论呈现出成长机会，也存在工作节奏方面的顾虑。',
                'pros':['有成长机会'], 'cons':['工作节奏可能较快']
            }, ensure_ascii=False)}]}
        ]}

    with patch.object(app.review, '_post_sse_json', side_effect=fake_post):
        result, provider = app.review.analyze('测试公司')
    assert result['score'] == 68 and result['label'] == '可以关注'
    assert result['sources'][0]['url'] == 'https://example.com/source'
    assert provider['name'] == '阿里云百炼联网搜索'
    assert calls[0][0] == 'https://dashscope.aliyuncs.com/compatible-mode/v1/responses'
    assert calls[0][1]['model'] == 'qwen3.8-flash'
    assert calls[0][1]['tools'] == [{'type':'web_search'}]
    assert '必须先使用 web_search' in calls[0][1]['instructions']
    assert calls[0][2]['Authorization'] == 'Bearer test-bailian-key'


def test_current_source_fixtures():
    research=Path(__file__).resolve().parents[1]/'research'
    if not (research/'open.json').exists():pytest.skip('Live-source fixture is kept on development server')
    jobs=parse_sheet(json.loads((research/'open.json').read_text()),'招聘信息',2026)
    events=parse_sheet(json.loads((research/'events.json').read_text()),'宣讲会信息',2026)
    assert len(jobs)==54 and len(events)==33
    assert any(r['company']=='单位名称待补充' for r in jobs)
    assert next(r for r in jobs if r['company']=='荣耀')['deadline_date']=='2026-09-30'
    assert all(r['date'] for r in events)
    assert next(r for r in events if '经纬恒润' in r['company'])['time_known'] is False
    assert next(r for r in jobs if r['company']=='中关村实验室')['apply_url'].startswith('mailto:')
