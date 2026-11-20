"""转赠 API 端到端：种子导入、页面、提交/领取/撤回/到期、幂等与阻断状态码。"""
import pytest
from fastapi.testclient import TestClient
from app import main
from app.modules.transfer import handler as transfer_handler
from app.modules.transfer.seeder import seed_if_empty
from app.modules.transfer.service import TransferService
from app.transfer_store import SqliteTransferRepository
from app.modules.transfer.domain import replay


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / 'workbench.sqlite')
    monkeypatch.setattr(main, 'DB_PATH', db)
    monkeypatch.setattr(transfer_handler, 'DB_PATH', db)
    # 首次访问自动播种，且播种幂等
    assert seed_if_empty(db) is True
    assert seed_if_empty(db) is False
    return TestClient(main.app)


def test_seed_preserves_baseline_balance_and_expiry(client):
    dash = client.get('/api/transfer/dashboard').json()
    # clean 基线 15 户 + 00001 的演示晚到期批次仍为 15 户
    assert len(dash['balances']) == 15
    member1 = next(b for b in dash['balances'] if b['member'] == '00001')
    # clean: 00001 剩余 30（100+80-120-50+30+20-... 重放结果）+ 演示批次 50
    assert member1['available'] == 80
    seed_lots = [l for l in dash['lots'] if l['member'] == '00001']
    # 原批次到期日（UTC 归一：北京 12-01 零点 = UTC 11-30 16:00）与演示批次同时保留
    assert sorted(l['expires'] for l in seed_lots) == [
        '2026-11-30T16:00:00+00:00', '2027-01-01T00:00:00+00:00']


def test_page_renders_status_balances_and_reasons(client):
    html = client.get('/transfer').text
    assert '积分转赠' in html
    assert '双方可用额' in html
    assert '阻断原因码' in html
    assert 'INSUFFICIENT_POINTS' in html
    assert '待领取' in html or '暂无转赠单' in html


def test_full_open_claim_flow_via_http(client):
    open_resp = client.post('/api/transfer/open', json={
        'transfer_id': 'T-HTTP-1', 'sender': '00001', 'recipient': '00002', 'amount': 25})
    assert open_resp.status_code == 200
    body = open_resp.json()
    assert body['ok'] is True and body['order']['status'] == '待领取'
    # 待领取：转出方减少 25，接收方不变
    dash = client.get('/api/transfer/dashboard').json()
    bal = {b['member']: b['available'] for b in dash['balances']}
    assert bal['00001'] == 55
    assert bal['00002'] == 70   # clean 基线 00002 = 100-40+10 = 70
    # 非接收方领取被拒
    forbidden = client.post('/api/transfer/claim',
                            json={'transfer_id': 'T-HTTP-1', 'actor': '00001'})
    assert forbidden.status_code == 403 and forbidden.json()['code'] == 'NOT_RECIPIENT'
    # 接收方领取后立即到账可消费
    claim = client.post('/api/transfer/claim',
                        json={'transfer_id': 'T-HTTP-1', 'actor': '00002'})
    assert claim.status_code == 200 and claim.json()['ok'] is True
    assert claim.json()['balances']['recipient']['available'] == 95
    # 领取到账批次沿用原到期日（UTC 归一存储，原值即北京 12-01 零点）
    dash = client.get('/api/transfer/dashboard').json()
    received = [l for l in dash['lots']
                if l['member'] == '00002' and l['lot_id'].startswith('T-HTTP-1')]
    assert received and all(l['expires'] == '2026-11-30T16:00:00+00:00' for l in received)


def test_revoke_flow_restores_sender(client):
    client.post('/api/transfer/open', json={
        'transfer_id': 'T-HTTP-2', 'sender': '00002', 'recipient': '00003', 'amount': 30})
    before = {b['member']: b['available']
              for b in client.get('/api/transfer/dashboard').json()['balances']}
    assert before['00002'] == 40
    # 非转出方撤回被拒
    assert client.post('/api/transfer/revoke',
                       json={'transfer_id': 'T-HTTP-2', 'actor': '00003'}).status_code == 403
    revoked = client.post('/api/transfer/revoke',
                          json={'transfer_id': 'T-HTTP-2', 'actor': '00002'})
    assert revoked.status_code == 200 and revoked.json()['ok'] is True
    after = {b['member']: b['available']
             for b in client.get('/api/transfer/dashboard').json()['balances']}
    assert after['00002'] == 70 and after['00003'] == 70
    order = client.get('/api/transfer/orders/T-HTTP-2').json()
    assert order['status'] == '已撤回' and order['version'] == 2


def test_retry_is_idempotent_no_new_credit(client):
    payload = {'transfer_id': 'T-HTTP-3', 'sender': '00003',
               'recipient': '00004', 'amount': 15}
    first = client.post('/api/transfer/open', json=payload)
    second = client.post('/api/transfer/open', json=payload)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()['code'] == 'IDEMPOTENT_HIT'
    dash = client.get('/api/transfer/dashboard').json()
    orders = [o for o in dash['orders'] if o['transfer_id'] == 'T-HTTP-3']
    assert len(orders) == 1
    # 没有重复冻结：00003 只少 15
    assert next(b for b in dash['balances'] if b['member'] == '00003')['available'] == 55


def test_insufficient_and_self_transfer_blocked(client):
    resp = client.post('/api/transfer/open', json={
        'transfer_id': 'T-HTTP-4', 'sender': '00001', 'recipient': '00002', 'amount': 99999})
    assert resp.status_code == 422 and resp.json()['code'] == 'INSUFFICIENT_POINTS'
    resp2 = client.post('/api/transfer/open', json={
        'transfer_id': 'T-HTTP-5', 'sender': '00001', 'recipient': '00001', 'amount': 1})
    assert resp2.status_code == 422 and resp2.json()['code'] == 'SELF_TRANSFER'
    # 全部被阻断，没有产生任何转赠单
    dash = client.get('/api/transfer/dashboard').json()
    assert dash['orders'] == []


def test_expired_escrow_claim_blocked_and_sweep(client):
    # 00001 转 20，走最早到期批次（2026-12-01）
    client.post('/api/transfer/open', json={
        'transfer_id': 'T-HTTP-6', 'sender': '00001', 'recipient': '00002', 'amount': 20})
    # 服务层在领取时刻比对原到期日；直接调用仓储+服务构造到期场景
    from app.modules.transfer.service import TransferService
    svc = TransferService(SqliteTransferRepository(transfer_handler.DB_PATH))
    result = svc.claim('T-HTTP-6', '00002', at='2026-12-02T00:00:00+00:00')
    assert result['blocked'] is True and result['code'] == 'ESCROW_EXPIRED'
    detail = client.get('/api/transfer/orders/T-HTTP-6').json()
    assert detail['status'] == '已到期'
    assert detail['block_code'] == 'ESCROW_EXPIRED'
    # 主界面仪表盘能看到阻断原因
    dash = client.get('/api/transfer/dashboard').json()
    row = next(o for o in dash['orders'] if o['transfer_id'] == 'T-HTTP-6')
    assert row['block_detail']


def test_concurrent_conflict_returns_409_and_preserves_balance(client):
    """HTTP 串行请求下用尾哈希模拟陈旧提交：409 且旧版余额可继续使用。"""
    from app.errors import BusinessError
    from app.modules.transfer.domain import make_event
    repo = SqliteTransferRepository(transfer_handler.DB_PATH)
    ledger = replay(repo.load_events())
    stale_tail = ledger.last_hash
    # 先让链尾前进一笔
    client.post('/api/transfer/open', json={
        'transfer_id': 'T-CONF-1', 'sender': '00005', 'recipient': '00006', 'amount': 5})
    stale_event = make_event(ledger, 'GRANT',
                             {'lot_id': 'STALE-1', 'member': '00005', 'points': 1,
                              'expires': '2027-01-01T00:00:00+00:00',
                              'source_lot': 'STALE-1'}, '2026-11-20T00:00:00+00:00')
    with pytest.raises(BusinessError) as exc:
        repo.commit([stale_event], expected_tail=stale_tail)
    assert exc.value.status == 409 and exc.value.code == 'STALE_VERSION'
    # 旧版余额结果仍可正常读取与继续操作
    dash = client.get('/api/transfer/dashboard').json()
    assert next(b for b in dash['balances'] if b['member'] == '00005')['available'] == 65
    ok = client.post('/api/transfer/open', json={
        'transfer_id': 'T-CONF-2', 'sender': '00005', 'recipient': '00006', 'amount': 5})
    assert ok.status_code == 200


def test_bad_payload_returns_structured_422(client):
    assert client.post('/api/transfer/open', json={
        'transfer_id': '  ', 'sender': '00001', 'recipient': '00002',
        'amount': 1}).status_code == 422
    resp = client.post('/api/transfer/claim', json={'transfer_id': 'X'})
    assert resp.status_code == 422 and resp.json()['code'] == 'BAD_PAYLOAD'


def test_history_survives_restart(client, tmp_path):
    """事件持久化在 SQLite，重新实例化服务后全部状态可重建。"""
    client.post('/api/transfer/open', json={
        'transfer_id': 'T-DURABLE', 'sender': '00007', 'recipient': '00008', 'amount': 12})
    client.post('/api/transfer/claim', json={'transfer_id': 'T-DURABLE', 'actor': '00008'})
    rebuilt = TransferService(SqliteTransferRepository(transfer_handler.DB_PATH)).dashboard()
    order = next(o for o in rebuilt['orders'] if o['transfer_id'] == 'T-DURABLE')
    assert order['status'] == '已领取'
    assert rebuilt['event_count'] >= 2 and len(rebuilt['last_hash']) == 64
