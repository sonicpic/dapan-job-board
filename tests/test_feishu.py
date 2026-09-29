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
        assert client.put('/api/admin/feishu/annotations/tbl1/missing', json={'status': '关注'}).status_code == 404
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
