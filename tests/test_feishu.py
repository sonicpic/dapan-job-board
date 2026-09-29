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
