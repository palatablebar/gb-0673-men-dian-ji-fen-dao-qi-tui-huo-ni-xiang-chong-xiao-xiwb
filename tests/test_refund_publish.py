"""退货回拨可发布性：乱序多笔部分退货、跨期过期、幂等重试、原子发布。"""
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.consumption.rules import replay
from copy import deepcopy


def _member_rows(state, member='00001'):
    return [dict(x) for x in state['records'] if x['member'] == member]


def _member_lots(state, member='00001'):
    return [x for x in state['lots'] if x['member'] == member]


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'publish.sqlite'))
    return TestClient(main.app)


# --- 乱序多次部分退货：累计结果与退货先后顺序无关，且不超过原消费 ---

def test_out_of_order_partial_refunds_invariant(clean):
    # 原消费 S1=120（E1=100 先到期，E2=20）。分三笔部分退货共 60。
    base = _member_rows(clean)
    refunds = [
        ('RA', 20, '2026-10-20T09:00:00+08:00'),   # 跨到期前
        ('RB', 25, '2026-11-02T09:00:00+08:00'),   # 跨 E1 到期后
        ('RC', 15, '2026-11-02T12:00:00+08:00'),
    ]
    order_a = [r for r in base if r['type'] != 'REFUND']
    order_a += [dict(base[3], id=i, points=p, at=t) for i, p, t in refunds]
    order_b = [r for r in base if r['type'] != 'REFUND']
    order_b += [dict(base[3], id=i, points=p, at=t) for i, p, t in reversed(refunds)]
    a = replay(order_a, _member_lots(clean), clean['context']['as_of'])
    b = replay(order_b, _member_lots(clean), clean['context']['as_of'])
    # 顺序不同，但每批次的最终剩余、累计恢复、过期损失完全一致。
    assert [(x['id'], x['remaining']) for x in a['lots']] == [(x['id'], x['remaining']) for x in b['lots']]
    assert sum(x['restored'] for x in a['refunds']) == sum(x['restored'] for x in b['refunds'])
    assert sum(x['expired_refund'] for x in a['refunds']) == sum(x['expired_refund'] for x in b['refunds'])
    spend = a['spends'][0]
    assert spend['refunded'] == 60 and spend['refunded'] <= spend['points']
    # 财务口径：累计解释仍锚定原消费 120，不会因退货顺序多出余额。
    assert sum(x['remaining'] for x in a['lots']) == sum(x['remaining'] for x in b['lots'])


def test_cumulative_refund_never_exceeds_original_spend(clean):
    rows = _member_rows(clean)
    rows += [
        dict(rows[3], id='R-X1', points=70, at='2026-10-20T09:00:00+08:00'),
        dict(rows[3], id='R-X2', points=50, at='2026-10-21T09:00:00+08:00'),  # 累计 120，恰好打满
        dict(rows[3], id='R-X3', points=1, at='2026-10-22T09:00:00+08:00'),   # 第 121 分必须拒绝
    ]
    r = replay(rows, _member_lots(clean), clean['context']['as_of'])
    assert r['errors'][-1][1] == 'REFUND_CAP'
    accepted = [x for x in r['refunds'] if x['id'] in {'R-X1', 'R-X2'}]
    assert sum(x['points'] for x in accepted) == 120
    assert r['spends'][0]['refunded'] == 120


def test_refund_across_lot_expiry_split(clean):
    # 到期前退 10（E2 逆序恢复），到期后退 30：E2 可恢复部分 + E1 全部记过期损失。
    rows = [r for r in _member_rows(clean) if r['type'] != 'REFUND']
    rows += [
        dict(clean['records'][3], id='R-EARLY', points=10, at='2026-10-20T09:00:00+08:00'),
        dict(clean['records'][3], id='R-LATE', points=30, at='2026-11-02T09:00:00+08:00'),
    ]
    r = replay(rows, _member_lots(clean), clean['context']['as_of'])
    early = next(x for x in r['refunds'] if x['id'] == 'R-EARLY')
    late = next(x for x in r['refunds'] if x['id'] == 'R-LATE')
    assert early['restored'] == 10 and early['expired_refund'] == 0
    # 逆序回拨：先 E2 剩余可恢复 10，再 E1 的 20 已过期，绝不复活。
    assert late['restored'] == 10 and late['expired_refund'] == 20
    assert [(a['lot'], a['points'], a['expired']) for a in late['allocations']] == [('E2', 10, False), ('E1', 20, True)]


# --- 同一退货重试不能重复恢复 ---

def test_duplicate_refund_replay_is_idempotent(clean):
    rows = _member_rows(clean)
    rows.append(dict(rows[3]))  # 与既有 R1 完全同编号的重试提交（绕过采信层去重，直验回放幂等）
    r = replay(rows, _member_lots(clean), clean['context']['as_of'])
    codes = [code for _, code, _ in r['errors']]
    assert 'REFUND_DUPLICATE' in codes
    # 只落账一次：恢复与过期损失不翻倍，余额不变。
    r1_refunds = [x for x in r['refunds'] if x['id'] == 'R1']
    assert len(r1_refunds) == 1
    baseline = replay(_member_rows(clean), _member_lots(clean), clean['context']['as_of'])
    assert sum(x['remaining'] for x in r['lots']) == sum(x['remaining'] for x in baseline['lots'])


# --- 审计行携带累计口径与批次轨迹，全部可追溯且批次守恒 ---

def test_refund_audit_cumulative_columns(sample):
    r1 = next(x for x in sample['refund_audit'] if x['id'] == 'R1')
    assert r1['spend_points'] == 120
    assert r1['cumulative_refunded'] == 30
    assert r1['code'] == 'VERIFIED'
    # 回拨轨迹：R1 的 30 = E2 恢复 20 + E1 过期 10，逐批次可追溯。
    trail = [x for x in sample['refund_trail'] if x['refund_id'] == 'R1']
    assert {(x['lot'], x['points'], x['status']) for x in trail} == {
        ('E2', 20, '已恢复可用'), ('E1', 10, '已过期不恢复')}
    assert all(x['balanced'] for x in sample['lot_trail'])


# --- 发布流程：成功、幂等重试、陈旧版本、失败保留旧版、原始数据不丢 ---

def test_publish_flow_happy_path_and_idempotent(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/publish', json={'revision': 1})
    assert first.status_code == 200 and first.json()['replayed'] is True
    # 同一结果重试发布：幂等，不产生新发布、不重复恢复。
    again = c.post(f'/api/runs/{rid}/publish', json={'revision': 1}).json()
    assert again['replayed'] is False and again['run_id'] == rid
    pub = c.get('/api/publication').json()
    assert pub['published'] and pub['publication']['run_id'] == rid
    # 页面同时呈现退货回拨、会员余额与发布状态。
    html = c.get(f'/refund?run_id={rid}').text
    assert '已发布' in html and '退货回拨' in html and '会员余额' in html


def test_publish_stale_revision_keeps_old_publication(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    first_run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid_a = first_run['manifest']['run_id']
    c.post(f'/api/runs/{rid_a}/publish', json={'revision': 1})
    # 复核推进版本后，用旧 revision 发布必须 409，旧版发布保持不变。
    c.post(f'/api/runs/{rid_a}/review', json={'revision': 1})
    stale = c.post(f'/api/runs/{rid_a}/publish', json={'revision': 1})
    assert stale.status_code == 409 and stale.json()['code'] == 'STALE_VERSION'
    pub = c.get('/api/publication').json()['publication']
    assert pub['run_id'] == rid_a and pub['revision'] == 1


def test_publish_failure_preserves_previous_version(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    good = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid_good = good['manifest']['run_id']
    c.post(f'/api/runs/{rid_good}/publish', json={'revision': 1})
    # 构造一个守恒被破坏的批次：发布被业务校验拒绝，已发布旧版原样保留。
    broken = deepcopy(good)
    broken['balance']['points_balanced'] = False
    broken['manifest']['run_id'] = 'brokenrun0000000'
    broken['manifest']['sha256'] = 'broken-digest'
    store.save(str(tmp_path / 'publish.sqlite'), broken)
    denied = c.post('/api/runs/brokenrun0000000/publish', json={'revision': 1})
    assert denied.status_code == 422 and denied.json()['code'] == 'PUBLISH_UNBALANCED'
    pub = c.get('/api/publication').json()['publication']
    assert pub['run_id'] == rid_good


def test_publish_never_mutates_original_run_snapshot(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    before = c.get(f'/api/runs/{rid}').json()
    c.post(f'/api/runs/{rid}/publish', json={'revision': 1})
    after = c.get(f'/api/runs/{rid}').json()
    # 原始消费记录与输入快照在发布前后逐字节一致，没有数据丢失或改写。
    assert after['input_snapshot'] == before['input_snapshot']
    assert after['point_ledger'] == before['point_ledger']
    assert after['manifest']['sha256'] == before['manifest']['sha256']


def test_withdraw_and_republish(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={'revision': 1})
    assert c.delete(f'/api/runs/{rid}/publish').status_code == 200
    assert c.get('/api/publication').json()['published'] is False
    republish = c.post(f'/api/runs/{rid}/publish', json={'revision': 1})
    assert republish.status_code == 200 and republish.json()['replayed'] is True


def test_published_export_contains_refund_trail(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'dirty'}).json()
    rid = run['manifest']['run_id']
    c.post(f'/api/runs/{rid}/publish', json={'revision': 1})
    z = c.get(f'/api/export/{rid}')
    assert z.status_code == 200
    import io, json, zipfile
    bundle = zipfile.ZipFile(io.BytesIO(z.content))
    trail = json.loads(bundle.read('refund_trail.json'))
    assert len(trail['refund_trail']) == len(run['refund_trail'])
    assert all(x['balanced'] for x in trail['lot_trail'])
