"""转赠服务：校验、冻结/划转/撤回规则、幂等与乐观并发。

所有写操作走“重建投影 → 校验 → 生成事件 → 整笔提交”，
任一校验失败不产生事件，旧版余额结果原样保留。
"""
from copy import deepcopy
from datetime import datetime, timezone
from app.errors import BusinessError
from app.modules.transfer.domain import (
    make_event, replay, BLOCK_REASONS,
    TERMINAL,
    TRANSFER_RULE_VERSION,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _fail(code, status=422):
    raise BusinessError(code, BLOCK_REASONS[code], status)


class TransferService:
    def __init__(self, repository):
        # repository 负责持久化：load_events() / commit(events, expected_tail)
        self.repo = repository

    # -------------------------------------------------------- 内部
    def _ledger(self):
        return replay(self.repo.load_events())

    def _commit(self, ledger, events):
        # 乐观并发：提交时尾哈希必须仍是读取时的尾哈希，否则整笔失败、余额不动。
        self.repo.commit(events, expected_tail=ledger.last_hash)
        return replay(self.repo.load_events())

    def _order(self, ledger, transfer_id):
        order = ledger.orders.get(transfer_id)
        if order is None:
            _fail('ORDER_NOT_FOUND', 404)
        return order

    @staticmethod
    def _expire_event(ledger, transfer_id, due_lines, at):
        # 深拷贝切断与投影 order.lines 的共享引用，事件 payload 落库后不可变、可验哈希。
        payload = {'transfer_id': transfer_id, 'lines': deepcopy(due_lines)}
        return make_event(ledger, 'EXPIRE', payload, at)

    def _terminal_block(self, order):
        # 终结单上的重复操作返回当前结果并带阻断原因，绝不重复记账。
        return {'blocked': True, 'code': 'ORDER_TERMINAL',
                'detail': BLOCK_REASONS['ORDER_TERMINAL'], 'order': order.to_dict()}

    # -------------------------------------------------------- 开户/发放
    def grant(self, member, points, expires, at=None, lot_id=None):
        """种子批次开户（由对账余额导入或演示初始化调用）。"""
        at = at or now_iso()
        if not isinstance(points, int) or points <= 0:
            _fail('BAD_AMOUNT')
        ledger = self._ledger()
        lot_id = lot_id or f'L{ledger.lot_seq + 1:05d}'
        if lot_id in ledger.lots:
            raise BusinessError('LOT_EXISTS', '批次已存在')
        event = make_event(ledger, 'GRANT',
                           {'lot_id': lot_id, 'member': str(member),
                            'points': points, 'expires': expires,
                            'source_lot': lot_id}, at)
        ledger = self._commit(ledger, [event])
        return ledger.lots[lot_id].to_dict()

    # -------------------------------------------------------- 提交转赠
    def open_transfer(self, transfer_id, sender, recipient, amount, at=None, idempotency_key=None):
        """提交即托管：FEFO 冻结，待领取期间双方都不可用。

        同一 transfer_id 重试命中已有单 → 幂等返回，不新增额度。
        """
        at = at or now_iso()
        amount = self._coerce_amount(amount)
        sender, recipient = str(sender), str(recipient)
        if sender == recipient:
            _fail('SELF_TRANSFER')
        ledger = self._ledger()

        existing = ledger.orders.get(transfer_id)
        if existing is not None:
            # 幂等：同单重试必须与原请求参数一致，冲突则拒绝但不记账。
            if (existing.sender, existing.recipient, existing.amount) != (sender, recipient, amount):
                raise BusinessError('IDEMPOTENT_CONFLICT',
                                    '转赠编号已存在且参数不一致，拒绝重复提交')
            return self._result(ledger, existing, blocked=True,
                                code='IDEMPOTENT_HIT',
                                detail=BLOCK_REASONS['IDEMPOTENT_HIT'])

        if not any(lot.member == sender for lot in ledger.lots.values()):
            _fail('MEMBER_UNKNOWN')
        if not any(lot.member == recipient for lot in ledger.lots.values()):
            _fail('MEMBER_UNKNOWN')
        available = ledger.available_lots(sender, at)
        total = sum(lot.available for lot in available)
        if total < amount:
            _fail('INSUFFICIENT_POINTS')

        # FEFO：最早到期批次先冻结，逐批分摊并完整保留原到期依据。
        rest = amount
        lines = []
        for lot in available:
            take = min(rest, lot.available)
            lines.append({'lot_id': lot.lot_id, 'points': take, 'expires': lot.expires})
            rest -= take
            if rest == 0:
                break
        payload = {'transfer_id': transfer_id, 'sender': sender, 'recipient': recipient,
                   'amount': amount, 'lines': lines,
                   'idempotency_key': idempotency_key or transfer_id}
        event = make_event(ledger, 'ESCROW', payload, at)
        ledger = self._commit(ledger, [event])
        return self._result(ledger, ledger.orders[transfer_id])

    # -------------------------------------------------------- 领取
    def claim(self, transfer_id, actor, at=None):
        at = at or now_iso()
        ledger = self._ledger()
        order = self._order(ledger, transfer_id)
        if order.status in TERMINAL:
            return self._result(ledger, order, blocked=True,
                                code='ORDER_TERMINAL', detail=BLOCK_REASONS['ORDER_TERMINAL'])
        if actor != order.recipient:
            _fail('NOT_RECIPIENT', 403)
        events = []
        # 先核销此刻已过原到期日的行（逐行，不影响未到期行）。
        due = [line for line in order.lines if not line.get('expired') and line['expires'] <= at]
        if due:
            expire_event = self._expire_event(ledger, transfer_id, due, at)
            events.append(expire_event)
            ledger.apply(expire_event)   # 推进投影，后续事件才能拿到正确链尾与 seq
        active = [line for line in order.lines if not line.get('expired') and line['expires'] > at]
        if not active:
            # 全部行均已到期：整单终结，领取被阻断。
            ledger = self._commit(ledger, events)
            order = ledger.orders[transfer_id]
            return self._result(ledger, order, blocked=True,
                                code='ESCROW_EXPIRED', detail=BLOCK_REASONS['ESCROW_EXPIRED'])
        lot_map = {line['lot_id']: f'{transfer_id}-C{idx}'
                   for idx, line in enumerate(active, 1)}
        events.append(make_event(ledger, 'CLAIM',
                                 {'transfer_id': transfer_id, 'lot_map': lot_map}, at))
        ledger = self._commit(ledger, events)
        result = self._result(ledger, ledger.orders[transfer_id])
        if due:
            # 部分到期、部分到账：成功但携带到期提示，界面可复核被核销的行。
            result['code'] = 'PARTIAL_EXPIRED'
            result['detail'] = f'{sum(l["points"] for l in due)} 分已过原到期日被核销，其余已到账'
        return result

    # -------------------------------------------------------- 撤回
    def revoke(self, transfer_id, actor, at=None):
        at = at or now_iso()
        ledger = self._ledger()
        order = self._order(ledger, transfer_id)
        if order.status in TERMINAL:
            return self._result(ledger, order, blocked=True,
                                code='ORDER_TERMINAL', detail=BLOCK_REASONS['ORDER_TERMINAL'])
        if actor != order.sender:
            _fail('NOT_SENDER', 403)
        events = []
        due = [line for line in order.lines if not line.get('expired') and line['expires'] <= at]
        if due:
            expire_event = self._expire_event(ledger, transfer_id, due, at)
            events.append(expire_event)
            ledger.apply(expire_event)
        active = [line for line in order.lines if not line.get('expired') and line['expires'] > at]
        if not active:
            ledger = self._commit(ledger, events)
            order = ledger.orders[transfer_id]
            return self._result(ledger, order, blocked=True,
                                code='ESCROW_EXPIRED', detail=BLOCK_REASONS['ESCROW_EXPIRED'])
        events.append(make_event(ledger, 'REVOKE', {'transfer_id': transfer_id}, at))
        ledger = self._commit(ledger, events)
        result = self._result(ledger, ledger.orders[transfer_id])
        if due:
            result['code'] = 'PARTIAL_EXPIRED'
            result['detail'] = f'{sum(l["points"] for l in due)} 分已到期核销，其余已原路退回'
        return result

    # -------------------------------------------------------- 到期清扫
    def sweep_expired(self, at=None):
        """把托管中已过原到期日的行统一核销（定时任务/手工入口共用）。

        逐行核销：部分到期的单子保持待领取，全部到期才终结。
        """
        at = at or now_iso()
        ledger = self._ledger()
        events = []
        for order in ledger.orders.values():
            if order.status in TERMINAL:
                continue
            due = [line for line in order.lines
                   if not line.get('expired') and line['expires'] <= at]
            if due:
                event = self._expire_event(ledger, order.transfer_id, due, at)
                events.append(event)
                ledger.apply(event)   # 逐事件推进链尾，保证 seq/prev_hash 连续
        if events:
            ledger = self._commit(ledger, events)
        return self.dashboard()

    # -------------------------------------------------------- 读模型
    def dashboard(self):
        ledger = self._ledger()
        snap = ledger.snapshot()
        snap['rule_version'] = TRANSFER_RULE_VERSION
        return snap

    def get_order(self, transfer_id):
        ledger = self._ledger()
        return self._order(ledger, transfer_id).to_dict()

    # -------------------------------------------------------- 辅助
    @staticmethod
    def _coerce_amount(amount):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            _fail('BAD_AMOUNT')
        return amount

    def _result(self, ledger, order, blocked=False, code='', detail=''):
        order_dict = order.to_dict() if hasattr(order, 'to_dict') else order
        balances = {b['member']: b['available'] for b in ledger.member_balances()}
        return {
            'ok': not blocked,
            'blocked': blocked,
            'code': code,
            'detail': detail or (BLOCK_REASONS[code] if code else ''),
            'order': order_dict,
            'balances': {
                'sender': {'member': order_dict['sender'],
                           'available': balances.get(order_dict['sender'], 0)},
                'recipient': {'member': order_dict['recipient'],
                              'available': balances.get(order_dict['recipient'], 0)},
            },
            'rule_version': TRANSFER_RULE_VERSION,
        }
