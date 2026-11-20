"""把对账批次余额导入转赠账本作为初始批次。

导入的是七模块重放后的最终批次（已扣消费、已计退款恢复、已核销自然到期），
每条 GRANT 保留原 lot 编号与原到期日，转赠链与消费链通过 source_lot 可互相追溯。
另补一笔晚到期演示批次，用于展示 FEFO 跨批次冻结。整个导入幂等。
"""
from app.engine import evaluate
from app.modules.transfer.service import TransferService, now_iso
from app.transfer_store import SqliteTransferRepository

# 演示用补充批次：与样例原批次不同到期日，验证 FEFO 先冻最早到期批次。
DEMO_EXTRA = {'member': '00001', 'points': 50, 'expires': '2027-01-01T00:00:00+00:00',
              'lot_id': 'DEMO-LATE-00001'}


def seed_if_empty(db_path, dataset='clean'):
    repo = SqliteTransferRepository(db_path)
    if repo.seeded():
        return False
    service = TransferService(repo)
    baseline = evaluate({'dataset': dataset})
    at = now_iso()
    for lot in baseline['point_ledger']['lots']:
        if lot['remaining'] <= 0:
            continue
        service.grant(lot['member'], lot['remaining'], lot['expires'],
                      at=at, lot_id=f"SEED-{lot['id']}")
    # 补充批次的会员在基线中必须已存在，避免凭空开户。
    if any(b['member'] == DEMO_EXTRA['member'] for b in service.dashboard()['balances']):
        service.grant(DEMO_EXTRA['member'], DEMO_EXTRA['points'], DEMO_EXTRA['expires'],
                      at=at, lot_id=DEMO_EXTRA['lot_id'])
    return True
