"""转赠领域与服务规则测试：托管/领取/撤回/到期、幂等、并发、哈希链、守恒。"""
import json
import sqlite3
import pytest
from app.errors import BusinessError
from app.modules.transfer.domain import (
    replay, verify_chain, STATUS_PENDING, STATUS_CLAIMED, STATUS_REVOKED,
    STATUS_EXPIRED,
)
from app.modules.transfer.service import TransferService
from app.transfer_store import SqliteTransferRepository, connect


@pytest.fixture
def svc(tmp_path):
    repo = SqliteTransferRepository(str(tmp_path / 'transfer.sqlite'))
    service = TransferService(repo)
    # 两户各两批：早批 11-30 到期、晚批 12-31 到期，验证 FEFO 与原到期保留。
    service.grant('00001', 100, '2026-11-30T00:00:00+00:00',
                  at='2026-11-01T00:00:00+00:00', lot_id='A1')
    service.grant('00001', 80, '2026-12-31T00:00:00+00:00',
                  at='2026-11-01T00:00:00+00:00', lot_id='A2')
    service.grant('00002', 60, '2026-12-15T00:00:00+00:00',
                  at='2026-11-01T00:00:00+00:00', lot_id='B1')
    return service


def _balance(ledger, member):
    return sum(l.available for l in ledger.lots.values() if l.member == member)


def test_escrow_freezes_and_single_owner(svc):
    """提交即托管：转出方可用额下降，接收方不增加，托管中双方都不可用这一份。"""
    svc.open_transfer('T1', '00001', '00002', 30, at='2026-11-10T00:00:00+00:00')
    ledger = replay(svc.repo.load_events())
    order = ledger.orders['T1']
    assert order.status == STATUS_PENDING
    assert _balance(ledger, '00001') == 150          # 180 - 30
    assert _balance(ledger, '00002') == 60           # 未领取，不增加
    # 冻结积分仍挂在转出方批次上但 available=0 部分，接收方无任何来自 T1 的批次
    assert sum(line['points'] for line in order.lines) == 30
    assert not any(l.member == '00002' and l.source_lot for l in ledger.lots.values()
                   if l.source_lot.startswith('A'))


def test_fefo_uses_earliest_expiry_first(svc):
    """冻结按最早到期批次分摊，并完整保留每批原到期依据。"""
    svc.open_transfer('T2', '00001', '00002', 120, at='2026-11-10T00:00:00+00:00')
    order = svc.get_order('T2')
    assert [line['lot_id'] for line in order['lines']] == ['A1', 'A2']
    assert order['lines'][0] == {'lot_id': 'A1', 'points': 100,
                                 'expires': '2026-11-30T00:00:00+00:00'}
    assert order['lines'][1]['points'] == 20


def test_claim_transfers_with_original_expiry(svc):
    """领取才划转：接收方新增批次沿用原 lot 到期日，领取后立即可消费。"""
    svc.open_transfer('T3', '00001', '00002', 120, at='2026-11-10T00:00:00+00:00')
    result = svc.claim('T3', '00002', at='2026-11-11T00:00:00+00:00')
    assert result['ok'] is True
    assert result['balances']['recipient']['available'] == 180   # 60 + 120
    ledger = replay(svc.repo.load_events())
    new_lots = [l for l in ledger.lots.values() if l.member == '00002' and l.source_lot in ('A1', 'A2')]
    assert sorted((l.source_lot, l.expires, l.available) for l in new_lots) == [
        ('A1', '2026-11-30T00:00:00+00:00', 100),
        ('A2', '2026-12-31T00:00:00+00:00', 20),
    ]
    # 全局守恒：总发放 240，任何时点可用+冻结/已划转之和不变
    assert sum(l.available for l in ledger.lots.values()) + 0 == 240 - 0
    assert sum(l.issued for l in ledger.lots.values() if l.member == '00002') == 180


def test_revoke_returns_to_original_lots(svc):
    """撤回原路退回原批次，到期承诺不变，转出方可用额恢复。"""
    svc.open_transfer('T4', '00001', '00002', 120, at='2026-11-10T00:00:00+00:00')
    before = replay(svc.repo.load_events())
    assert _balance(before, '00001') == 60
    result = svc.revoke('T4', '00001', at='2026-11-12T00:00:00+00:00')
    assert result['ok'] is True
    assert result['balances']['sender']['available'] == 180
    ledger = replay(svc.repo.load_events())
    assert ledger.orders['T4'].status == STATUS_REVOKED
    assert (ledger.lots['A1'].available, ledger.lots['A2'].available) == (100, 80)
    assert ledger.lots['A1'].expires == '2026-11-30T00:00:00+00:00'


def test_conservation_through_all_states(svc):
    """待领取/已领取/撤回后，全系统总可用额与发放额守恒。"""
    total_issued = 240
    svc.open_transfer('T5', '00001', '00002', 50, at='2026-11-10T00:00:00+00:00')
    pending = replay(svc.repo.load_events())
    # 待领取：冻结 50 不在任何一方可用额
    assert sum(l.available for l in pending.lots.values()) == total_issued - 50
    svc.revoke('T5', '00001', at='2026-11-11T00:00:00+00:00')
    revoked = replay(svc.repo.load_events())
    assert sum(l.available for l in revoked.lots.values()) == total_issued
    svc.open_transfer('T6', '00001', '00002', 50, at='2026-11-13T00:00:00+00:00')
    svc.claim('T6', '00002', at='2026-11-14T00:00:00+00:00')
    claimed = replay(svc.repo.load_events())
    assert sum(l.available for l in claimed.lots.values()) == total_issued


def test_retry_same_transfer_is_idempotent(svc):
    """同一转赠重试不新增额度，返回幂等阻断标记。"""
    first = svc.open_transfer('T7', '00001', '00002', 40, at='2026-11-10T00:00:00+00:00')
    events_after_first = len(svc.repo.load_events())
    second = svc.open_transfer('T7', '00001', '00002', 40, at='2026-11-10T01:00:00+00:00')
    assert second['blocked'] is True
    assert second['code'] == 'IDEMPOTENT_HIT'
    assert len(svc.repo.load_events()) == events_after_first
    ledger = replay(svc.repo.load_events())
    assert _balance(ledger, '00001') == 140
    # 参数冲突的同号重试被拒绝且不记账
    with pytest.raises(BusinessError) as exc:
        svc.open_transfer('T7', '00001', '00002', 41, at='2026-11-10T02:00:00+00:00')
    assert exc.value.code == 'IDEMPOTENT_CONFLICT'
    assert len(svc.repo.load_events()) == events_after_first


def test_claim_and_revoke_terminal_idempotent(svc):
    """已领取/已撤回单上重复操作不重复记账。"""
    svc.open_transfer('T8', '00001', '00002', 20, at='2026-11-10T00:00:00+00:00')
    svc.claim('T8', '00002', at='2026-11-11T00:00:00+00:00')
    again = svc.claim('T8', '00002', at='2026-11-12T00:00:00+00:00')
    assert again['blocked'] is True and again['code'] == 'ORDER_TERMINAL'
    revoke_attempt = svc.revoke('T8', '00001', at='2026-11-12T00:00:00+00:00')
    assert revoke_attempt['blocked'] is True
    ledger = replay(svc.repo.load_events())
    assert sum(1 for e in svc.repo.load_events() if e['type'] == 'CLAIM') == 1


def test_expired_escrow_blocks_claim_and_does_not_revive(svc):
    """托管中过原到期日：领取被阻断，积分核销不复活，撤回同样阻断。"""
    svc.open_transfer('T9', '00001', '00002', 100, at='2026-11-10T00:00:00+00:00')
    result = svc.claim('T9', '00002', at='2026-12-01T00:00:00+00:00')
    assert result['blocked'] is True
    assert result['code'] == 'ESCROW_EXPIRED'
    ledger = replay(svc.repo.load_events())
    assert ledger.orders['T9'].status == STATUS_EXPIRED
    assert _balance(ledger, '00001') == 80       # 早批 100 已核销，晚批 80 仍在
    assert _balance(ledger, '00002') == 60
    revoke = svc.revoke('T9', '00001', at='2026-12-02T00:00:00+00:00')
    # 已到期单已终结，撤回返回终结阻断，积分仍不复活
    assert revoke['blocked'] is True and revoke['code'] == 'ORDER_TERMINAL'
    assert _balance(replay(svc.repo.load_events()), '00001') == 80


def test_partial_expiry_keeps_remaining_lines_claimable(svc):
    """跨批次转赠：早批到期逐行核销，晚批仍可领取且到期依据各自保留。"""
    # 120 = 早批 A1(11-30) 100 + 晚批 A2(12-31) 20
    svc.open_transfer('TP1', '00001', '00002', 120, at='2026-11-10T00:00:00+00:00')
    # 在早批已到期、晚批未到期之间领取
    result = svc.claim('TP1', '00002', at='2026-12-05T00:00:00+00:00')
    assert result['ok'] is True
    assert result['code'] == 'PARTIAL_EXPIRED'
    ledger = replay(svc.repo.load_events())
    order = ledger.orders['TP1']
    # 单子已终结为已领取（存活行全部到账），早批行留痕为 expired
    assert order.status == STATUS_CLAIMED
    expired_line = next(l for l in order.lines if l['lot_id'] == 'A1')
    claimed_line = next(l for l in order.lines if l['lot_id'] == 'A2')
    assert expired_line.get('expired') is True
    assert not claimed_line.get('expired')
    # 接收方只拿到晚批 20，沿用 12-31 到期；早批 100 已核销无人可用
    new_lot = next(l for l in ledger.lots.values()
                   if l.member == '00002' and l.source_lot == 'A2')
    assert (new_lot.available, new_lot.expires) == (20, '2026-12-31T00:00:00+00:00')
    assert _balance(ledger, '00002') == 80       # 60 自有 + 20 到账
    assert _balance(ledger, '00001') == 60       # 仅剩晚批 A2 余下 60
    # 守恒：发放 240 = 当前可用 200 + 到期核销 40（本测 100 中... 实际仅核销 100）
    assert sum(l.available for l in ledger.lots.values()) + 100 == 240


def test_partial_expiry_then_revoke_returns_only_live_lines(svc):
    svc.open_transfer('TP2', '00001', '00002', 120, at='2026-11-10T00:00:00+00:00')
    result = svc.revoke('TP2', '00001', at='2026-12-05T00:00:00+00:00')
    assert result['ok'] is True and result['code'] == 'PARTIAL_EXPIRED'
    ledger = replay(svc.repo.load_events())
    assert ledger.orders['TP2'].status == STATUS_REVOKED
    # 早批 100 已核销不退；晚批 20 原路退回 A2（A2 原 80，冻 20 后余 60，退回得 80）
    assert ledger.lots['A1'].available == 0
    assert ledger.lots['A2'].available == 80


def test_sweep_expires_due_orders(svc):
    svc.open_transfer('T10', '00001', '00002', 100, at='2026-11-10T00:00:00+00:00')
    svc.open_transfer('T11', '00001', '00002', 10, at='2026-11-10T00:00:00+00:00')
    # T10 全冻早批（11-30 到期）；T11 冻晚批（12-31 到期），清扫只核销已到期的 T10
    dash = svc.sweep_expired(at='2026-12-01T00:00:00+00:00')
    statuses = {o['transfer_id']: o['status'] for o in dash['orders']}
    assert statuses['T10'] == STATUS_EXPIRED
    assert statuses['T11'] == STATUS_PENDING


def test_blocking_reasons(svc):
    with pytest.raises(BusinessError) as e1:
        svc.open_transfer('X1', '00001', '00001', 10)
    assert e1.value.code == 'SELF_TRANSFER'
    with pytest.raises(BusinessError) as e2:
        svc.open_transfer('X2', '00001', '00002', 0)
    assert e2.value.code == 'BAD_AMOUNT'
    with pytest.raises(BusinessError) as e3:
        svc.open_transfer('X3', '00001', '00002', 9999)
    assert e3.value.code == 'INSUFFICIENT_POINTS'
    with pytest.raises(BusinessError) as e4:
        svc.open_transfer('X4', '00001', '99999', 10)
    assert e4.value.code == 'MEMBER_UNKNOWN'
    svc.open_transfer('X5', '00001', '00002', 10, at='2026-11-10T00:00:00+00:00')
    with pytest.raises(BusinessError) as e5:
        svc.claim('X5', '00001')
    assert e5.value.code == 'NOT_RECIPIENT'
    with pytest.raises(BusinessError) as e6:
        svc.revoke('X5', '00002')
    assert e6.value.code == 'NOT_SENDER'
    with pytest.raises(BusinessError) as e7:
        svc.claim('NOPE', '00002')
    assert e7.value.status == 404 and e7.value.code == 'ORDER_NOT_FOUND'


def test_optimistic_concurrency_keeps_old_balance(svc, tmp_path):
    """尾哈希乐观锁：并发提交只有一笔成功，失败方旧版余额原样保留。"""
    path = str(tmp_path / 'transfer.sqlite')
    repo_a = SqliteTransferRepository(path)
    repo_b = SqliteTransferRepository(path)
    sa, sb = TransferService(repo_a), TransferService(repo_b)
    # 两边都读到空链尾
    la = replay(repo_a.load_events())
    lb = replay(repo_b.load_events())
    from app.modules.transfer.domain import make_event
    ev_a = make_event(la, 'GRANT', {'lot_id': 'W1', 'member': '00001', 'points': 10,
                                    'expires': '2027-01-01T00:00:00+00:00',
                                    'source_lot': 'W1'}, '2026-11-10T00:00:00+00:00')
    ev_b = make_event(lb, 'GRANT', {'lot_id': 'W2', 'member': '00002', 'points': 20,
                                    'expires': '2027-01-01T00:00:00+00:00',
                                    'source_lot': 'W2'}, '2026-11-10T00:00:00+00:00')
    repo_a.commit([ev_a], expected_tail=la.last_hash)
    with pytest.raises(BusinessError) as exc:
        repo_b.commit([ev_b], expected_tail=lb.last_hash)
    assert exc.value.code == 'STALE_VERSION' and exc.value.status == 409
    # 旧版结果未被部分写入：只有 W1 落账，W2 不存在
    ledger = replay(repo_a.load_events())
    assert set(ledger.lots) == {'A1', 'A2', 'B1', 'W1'}
    # 失败方重读后可在新版上重试成功
    sb.grant('00002', 20, '2027-01-01T00:00:00+00:00',
             at='2026-11-10T01:00:00+00:00', lot_id='W2')
    assert set(replay(repo_a.load_events()).lots) == {'A1', 'A2', 'B1', 'W1', 'W2'}


def test_hash_chain_detects_tamper_and_rewrite(svc):
    """历史覆盖/篡改：验链直接失败，争议依据不可被静默改写。"""
    svc.open_transfer('T12', '00001', '00002', 10, at='2026-11-10T00:00:00+00:00')
    events = svc.repo.load_events()
    assert verify_chain(events) is True
    # 1) 篡改载荷但保留哈希
    tampered = json.loads(json.dumps(events))
    escrow = next(e for e in tampered if e['type'] == 'ESCROW')
    escrow['payload']['amount'] = 999
    with pytest.raises(ValueError):
        verify_chain(tampered)
    # 2) 物理覆盖存储中的历史行，验链同样失败
    path = svc.repo.path
    with connect(path) as con:
        con.execute("UPDATE transfer_events SET payload=json_set(payload,'$.amount',999) WHERE type='ESCROW'")
    with pytest.raises(ValueError):
        replay(SqliteTransferRepository(path).load_events())


def test_events_are_append_only_no_update_path(svc):
    """存储层只暴露 INSERT：表内不存在可改写业务字段的更新入口。"""
    svc.open_transfer('T13', '00001', '00002', 10, at='2026-11-10T00:00:00+00:00')
    path = svc.repo.path
    with connect(path) as con:
        cols = con.execute("PRAGMA table_info(transfer_events)").fetchall()
        assert {c['name'] for c in cols} >= {'seq', 'type', 'payload', 'hash', 'prev_hash'}
        # seq 严格连续，无软删标记列
        seqs = [r['seq'] for r in con.execute('SELECT seq FROM transfer_events ORDER BY seq')]
        assert seqs == list(range(1, len(seqs) + 1))
    # 仓储公共方法只有读与整笔追加
    public = [n for n in dir(SqliteTransferRepository) if not n.startswith('_')]
    assert 'delete' not in ''.join(public) and 'update' not in ''.join(public)
