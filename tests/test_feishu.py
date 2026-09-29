import datetime as dt
import json
from unittest.mock import patch

from test_app import client, sign_in
import app
import feishu


def sample_snapshot(records=None):
    return {'base_token': 'app-test', 'tables': [{
        'table': {'table_id': 'tbl1', 'name': '资料'},
        'fields': [{'field_id': 'fld1', 'field_name': '名称', 'type': 1}],
        'views': [{'view_id': 'vew1', 'view_name': '全部'}],
        'records': records if records is not None else [{'record_id': 'rec1', 'fields': {'名称': '第一条'}}],
    }]}


def test_admin_only_and_public_isolation(client):
    assert client.get('/api/admin/feishu/manifest').status_code == 401
    assert client.get('/api/admin/feishu/records').status_code == 401
    sign_in(client)
    with patch.object(feishu, 'snapshot', return_value=sample_snapshot()), \
         patch.object(feishu, 'target_config', return_value={'base_token': 'app-test', 'url': '', 'table_id': '', 'view_id': ''}):
        assert app.run_feishu_sync()
        result = client.get('/api/admin/feishu/records').json()
        assert result['total'] == 1
        assert result['items'][0]['fields']['名称'] == '第一条'
        assert client.get('/api/admin/feishu/records?q=第一').json()['total'] == 1
        assert client.get('/api/admin/feishu/manifest').json()['tables'][0]['fields'][0]['field_id'] == 'fld1'
        assert '第一条' not in json.dumps(client.get('/api/public').json(), ensure_ascii=False)


def test_failed_sync_preserves_last_good_and_source_deletion_is_retained(client):
    with patch.object(feishu, 'snapshot', return_value=sample_snapshot()):
        assert app.run_feishu_sync()
    with patch.object(feishu, 'snapshot', side_effect=feishu.FeishuError('permission denied')):
        assert not app.run_feishu_sync()
    with app.conn() as c:
        assert c.execute('SELECT count(*) FROM feishu_records WHERE source_missing=0').fetchone()[0] == 1
        assert c.execute('SELECT status FROM feishu_sync_runs ORDER BY id DESC LIMIT 1').fetchone()[0] == 'error'
    with patch.object(feishu, 'snapshot', return_value=sample_snapshot([])):
        assert app.run_feishu_sync()
    with app.conn() as c:
        assert c.execute('SELECT count(*) FROM feishu_records WHERE source_missing=1').fetchone()[0] == 1


def test_public_field_values_are_resolved_for_admin_search():
    cells = {'fld1': {'value': [{'text': '第一条', 'type': 'text'}]},
             'fld2': {'value': ['opt-a']}}
    fields = [{'field_id': 'fld1', 'name': '名称', 'type': 1},
              {'field_id': 'fld2', 'name': '分类', 'type': 4,
               'property': {'options': [{'id': 'opt-a', 'name': '公开'}]}}]
    assert feishu.readable_fields(cells, fields) == {'名称': '第一条', '分类': '公开'}


def test_deadlines_are_normalized_without_model_calls():
    assert app.normalize_feishu_deadline('2026/10//31')[1:] == ('2026-10-31', '2026-10-31')
    assert app.normalize_feishu_deadline('2026年4月23日')[1:] == ('2026-04-23', '2026-04-23')
    assert app.normalize_feishu_deadline('26.3.10')[1:] == ('2026-03-10', '2026-03-10')
    assert app.normalize_feishu_deadline('6月18日', 2026)[1:] == ('2026-06-18', '2026-06-18')
    assert app.normalize_feishu_deadline('202610/9')[1:] == ('2026-10-09', '2026-10-09')
    assert app.normalize_feishu_deadline('尽快投递')[1:] == ('尽快投递', '0000-01-01')
    assert app.normalize_feishu_deadline('招满即止')[1:] == ('招满即止', '0000-01-02')
    assert app.feishu_deadline_bounds(3, dt.date(2026, 10, 29)) == ('2026-10-29', '2026-10-31')


def test_upcoming_deadline_window_excludes_past_future_and_undated_records(client):
    today = dt.datetime.now(app.TZ).date()
    dated = [(-1, 'past'), (0, 'today'), (1, 'tomorrow'), (2, 'day-three'), (3, 'later')]
    records = [{'record_id': key, 'fields': {'公司名称': key, '网申截止': (today + dt.timedelta(days=offset)).isoformat()}}
               for offset, key in dated]
    records.append({'record_id': 'urgent', 'fields': {'公司名称': 'urgent', '网申截止': '尽快投递'}})
    records.append({'record_id': 'unknown', 'fields': {'公司名称': 'unknown', '网申截止': '待定'}})
    config = {'base_token': 'app-test', 'url': '', 'table_id': 'tbl1', 'view_id': 'vew1'}
    with patch.object(feishu, 'target_config', return_value=config), \
         patch.object(feishu, 'snapshot', return_value=sample_snapshot(records)):
        assert app.run_feishu_sync()
        sign_in(client)
        response = client.get('/api/admin/feishu/records', params={
            'deadline_days': 3, 'sort': 'company', 'direction': 'desc', 'group_by': 'industry'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['total'] == 3
        assert [item['record_id'] for item in result['items']] == ['today', 'tomorrow', 'day-three']
        assert (result['sort'], result['direction'], result['group_by']) == ('deadline', 'asc', '')
        page = client.get('/api/admin/feishu/records', params={'deadline_days': 3, 'limit': 1, 'offset': 1}).json()
        assert page['total'] == 3
        assert [item['record_id'] for item in page['items']] == ['tomorrow']
        assert client.get('/api/admin/feishu/records?deadline_days=366').status_code == 422


def test_job_search_filters_sort_and_annotations_are_admin_only(client):
    records = [
        {'record_id': 'rec-a', 'fields': {'公司名称': '星辰科技', '校招岗位': '后端开发', '公司行业': '互联网',
                                         '企业性质': '民企', '招聘类型': '校招', '工作地点': '北京、上海',
                                         '学历': '本科起', '是否笔试': '是', '网申更新': '2026-09-20 00:00'}},
        {'record_id': 'rec-b', 'fields': {'公司名称': '海岳集团', '校招岗位': '产品经理', '公司行业': '金融',
                                         '企业性质': '国企', '招聘类型': '实习', '工作地点': '深圳',
                                         '学历': '硕士起', '是否笔试': '否', '网申更新': '2026-09-25 00:00'}},
    ]
    config = {'base_token': 'app-test', 'url': '', 'table_id': 'tbl1', 'view_id': 'vew1'}
    with patch.object(feishu, 'target_config', return_value=config), \
         patch.object(feishu, 'snapshot', return_value=sample_snapshot(records)):
        assert app.run_feishu_sync()
        path = '/api/admin/feishu/annotations/tbl1/rec-a'
        assert client.put(path, json={'status': '关注', 'priority': 3, 'tags': ['北京', '后端', '北京'], 'note': '已联系校友'}).status_code == 401
        assert client.get('/api/admin/feishu/annotations').status_code == 401
        sign_in(client)
        assert client.put(path, json={'status': '无效状态'}).status_code == 422
        assert client.put('/api/admin/feishu/annotations/tbl1/missing', json={'status': '关注', 'priority': 1}).status_code == 404
        saved = client.put(path, json={'status': '关注', 'priority': 3, 'tags': ['北京', '后端', '北京'], 'note': '已联系校友'})
        assert saved.status_code == 200
        assert saved.json()['tags'] == ['北京', '后端']
        assert client.get('/api/admin/feishu/annotations').json()['items'][0]['note'] == '已联系校友'
        def search(params):
            response = client.get('/api/admin/feishu/records', params=params)
            assert response.status_code == 200, response.text
            return response.json()
        assert [r['record_id'] for r in search({'sort': 'updated_date', 'direction': 'desc'})['items']] == ['rec-b', 'rec-a']
        assert search({'q': '后端开发'})['total'] == 1
        assert search({'q': '%'})['total'] == 0
        assert search({'industry': '互联网', 'location': '北京', 'education': '本科起', 'exam': '是'})['total'] == 1
        result = search({'status': '关注', 'priority': 3, 'tag': '北京', 'group_by': 'annotation_status'})
        assert result['total'] == 1
        assert result['items'][0]['group_value'] == '关注'
        assert result['items'][0]['annotation']['note'] == '已联系校友'
        assert result['facets']['tags'] == {'北京': 1, '后端': 1}
        assert search({'status': '待筛选'})['items'][0]['record_id'] == 'rec-b'
        assert '星辰科技' not in json.dumps(client.get('/api/public').json(), ensure_ascii=False)


def test_search_index_backfills_only_missing_rows(client):
    with patch.object(feishu, 'snapshot', return_value=sample_snapshot([
        {'record_id': 'rec-a', 'fields': {'公司名称': '测试一'}},
        {'record_id': 'rec-b', 'fields': {'公司名称': '测试二'}},
    ])):
        assert app.run_feishu_sync()
    with app.conn() as c:
        c.execute("DELETE FROM feishu_record_search WHERE record_id='rec-b'")
    sign_in(client)
    with patch.object(feishu, 'target_config', return_value={'base_token': 'app-test', 'url': '', 'table_id': '', 'view_id': ''}):
        assert client.get('/api/admin/feishu/records').json()['total'] == 2


def test_multiselect_filters_and_true_deadline_sort(client):
    records = [
        {'record_id': 'rec-urgent', 'fields': {'公司名称': '甲', '公司行业': '互联网', '工作地点': '北京', '网申截止': '尽快投递'}},
        {'record_id': 'rec-late', 'fields': {'公司名称': '乙', '公司行业': '金融', '工作地点': '上海', '网申截止': '2026/10//31'}},
        {'record_id': 'rec-early', 'fields': {'公司名称': '丙', '公司行业': '互联网', '工作地点': '深圳', '网申截止': '2026年4月23日'}},
    ]
    config = {'base_token': 'app-test', 'url': '', 'table_id': 'tbl1', 'view_id': 'vew1'}
    with patch.object(feishu, 'target_config', return_value=config), \
         patch.object(feishu, 'snapshot', return_value=sample_snapshot(records)):
        assert app.run_feishu_sync()
        sign_in(client)
        result = client.get('/api/admin/feishu/records', params={'sort': 'deadline', 'direction': 'asc'}).json()
        assert [item['record_id'] for item in result['items']] == ['rec-urgent', 'rec-early', 'rec-late']
        assert result['items'][-1]['deadline']['label'] == '2026-10-31'
        result = client.get('/api/admin/feishu/records', params=[('industry', '互联网'), ('industry', '金融'), ('location', '北京'), ('location', '上海')]).json()
        assert result['total'] == 2
        assert set(result['facets']['location']) >= {'北京', '上海', '深圳'}
        result = client.get('/api/admin/feishu/records', params={'industry': '互联网'}).json()
        assert set(result['facets']['location']) == {'北京', '深圳'}
        assert set(result['facets']['industry']) == {'互联网', '金融'}
        path = '/api/admin/feishu/annotations/tbl1/rec-late'
        assert client.put(path, json={'status': '关注', 'priority': 0}).status_code == 422
        assert client.put(path, json={'status': '关注', 'priority': 3}).status_code == 200
        result = client.get('/api/admin/feishu/records', params={'focus': 3}).json()
        assert [item['record_id'] for item in result['items']] == ['rec-late']


def test_all_facet_options_are_available_and_filter_counts_match(client):
    records = [
        {'record_id': f'city-{index}', 'fields': {'公司名称': f'公司{index}', '工作地点': f'城市{index}'}}
        for index in range(100)
    ] + [
        {'record_id': 'xiong-an', 'fields': {'公司名称': '甲', '工作地点': '北京、雄安'}},
        {'record_id': 'xiong-an-long', 'fields': {'公司名称': '乙', '工作地点': '河北雄安'}},
        {'record_id': 'blank', 'fields': {'公司名称': '丙', '工作地点': ''}},
        {'record_id': 'beijing-city', 'fields': {'公司名称': '丁', '工作地点': '北京市'}},
        {'record_id': 'slash', 'fields': {'公司名称': '戊', '工作地点': '广州/深圳'}},
        {'record_id': 'comma', 'fields': {'公司名称': '己', '工作地点': '成都，杭州'}},
    ]
    config = {'base_token': 'app-test', 'url': '', 'table_id': 'tbl1', 'view_id': 'vew1'}
    with patch.object(feishu, 'target_config', return_value=config), \
         patch.object(feishu, 'snapshot', return_value=sample_snapshot(records)):
        assert app.run_feishu_sync()
        sign_in(client)
        def search(params):
            response = client.get('/api/admin/feishu/records', params=params)
            assert response.status_code == 200, response.text
            return response.json()
        facets = search({})['facets']['location']
        assert len(facets) == 109
        assert facets['雄安'] == 1
        assert facets['未填写'] == 1
        assert search({'location': '雄安'})['total'] == facets['雄安']
        assert search({'location': '北京'})['total'] == facets['北京']
        assert search({'location': '未填写'})['total'] == facets['未填写']
        assert search({'location': '广州'})['total'] == facets['广州'] == 1
        assert search({'location': '杭州'})['total'] == facets['杭州'] == 1
        assert search({'q': '雄安'})['total'] == 2
        many_locations = [('location', f'城市{index}') for index in range(21)]
        assert search(many_locations)['total'] == 21
        for index in range(21):
            assert client.put(f'/api/admin/feishu/annotations/tbl1/city-{index}', json={
                'status': '待筛选', 'tags': [f'标签{index}'],
            }).status_code == 200
        many_tags = [('tag', f'标签{index}') for index in range(21)]
        assert search(many_tags)['total'] == 21
