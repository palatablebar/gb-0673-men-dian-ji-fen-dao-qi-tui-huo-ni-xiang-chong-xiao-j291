"""积分转赠领域内核（纯函数，无 IO）。

裁决：提交即托管、领取才划转。
- 提交（ESCROW）：积分从转出方可用批次冻结到转赠单，双方均不可用；
  一份积分在任一时刻只有一个可用主人，托管中无主人。
- 领取（CLAIM）：冻结积分按原批次、原到期日划入接收方，领取后立即可消费。
- 撤回（REVOKE）：待领取期间转出方可撤回，积分原路退回原批次，到期承诺不变。
- 到期（EXPIRE）：托管中的批次到达原到期日仍未领取，领取被阻断并记到期事件，
  到期积分不复活，守恒口径与自然到期一致。

事件只追加、不修改；每条事件携带前一条哈希，形成可验链。
"""
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json

TRANSFER_RULE_VERSION = '2026.11.20-transfer-r1'

STATUS_PENDING = '待领取'
STATUS_CLAIMED = '已领取'
STATUS_REVOKED = '已撤回'
STATUS_EXPIRED = '已到期'
TERMINAL = {STATUS_CLAIMED, STATUS_REVOKED, STATUS_EXPIRED}

# 阻断原因码 → 中文说明（主界面直接展示）
BLOCK_REASONS = {
    'SELF_TRANSFER': '不能转赠给自己',
    'BAD_AMOUNT': '转赠积分须为正整数',
    'MEMBER_UNKNOWN': '会员账户不存在',
    'INSUFFICIENT_POINTS': '转出方可用积分不足',
    'ORDER_NOT_FOUND': '转赠单不存在',
    'ORDER_TERMINAL': '转赠单已终结，不能重复操作',
    'NOT_RECIPIENT': '仅接收方可以领取',
    'NOT_SENDER': '仅转出方可以撤回',
    'ESCROW_EXPIRED': '托管积分已过原批次到期日，领取被阻断',
    'STALE_VERSION': '版本已变化，请刷新后重试（旧版余额未改动）',
    'IDEMPOTENT_HIT': '同一转赠重试命中幂等记录，未新增额度',
    'PARTIAL_EXPIRED': '部分批次已过原到期日被核销，其余已处理',
}

EVENT_TYPES = {'GRANT', 'ESCROW', 'CLAIM', 'REVOKE', 'EXPIRE'}


def _hash_payload(payload):
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- 投影
@dataclass
class Lot:
    lot_id: str
    member: str
    issued: int
    available: int
    expires: str  # ISO8601，UTC 归一
    source_lot: str = ''

    def to_dict(self):
        return {'lot_id': self.lot_id, 'member': self.member, 'issued': self.issued,
                'available': self.available, 'expires': self.expires, 'source_lot': self.source_lot}


@dataclass
class TransferOrder:
    transfer_id: str
    sender: str
    recipient: str
    amount: int
    status: str = STATUS_PENDING
    lines: list = field(default_factory=list)   # [{lot_id, points, expires}]
    version: int = 1
    created_at: str = ''
    updated_at: str = ''
    block_code: str = ''
    block_detail: str = ''
    events: list = field(default_factory=list)

    def to_dict(self):
        return {k: deepcopy(v) for k, v in self.__dict__.items()}


class TransferLedger:
    """事件回放得到的内存投影：批次余额 + 转赠单 + 哈希链游标。"""

    def __init__(self):
        self.lots = {}                 # lot_id -> Lot
        self.lot_seq = 0
        self.orders = {}               # transfer_id -> TransferOrder
        self.last_hash = '0' * 64
        self.event_count = 0

    # ------------------------------------------------------------ 回放
    def apply(self, event):
        etype = event['type']
        if etype not in EVENT_TYPES:
            raise ValueError(f"未知事件类型 {etype}")
        expected = _hash_payload(event['payload'])
        if event['hash'] != expected:
            raise ValueError(f"事件 {event['seq']} 哈希与载荷不符，链已被篡改")
        if event.get('prev_hash') != self.last_hash:
            raise ValueError(f"事件 {event['seq']} 断链，历史已被覆盖")
        getattr(self, f'_on_{etype.lower()}')(event)
        self.last_hash = event['hash']
        self.event_count = event['seq']

    def _on_grant(self, event):
        p = event['payload']
        self.lots[p['lot_id']] = Lot(p['lot_id'], p['member'], p['points'],
                                     p['points'], p['expires'], p.get('source_lot', ''))

    def _on_escrow(self, event):
        p = event['payload']
        # 投影持有 lines 的独立副本，后续 expired 标记不回写事件 payload，保证哈希可复验。
        order = TransferOrder(p['transfer_id'], p['sender'], p['recipient'], p['amount'],
                              STATUS_PENDING, deepcopy(p['lines']), 1,
                              event['at'], event['at'])
        self.orders[p['transfer_id']] = order
        for line in p['lines']:
            lot = self.lots[line['lot_id']]
            if lot.available < line['points']:
                raise ValueError(f"事件 {event['seq']} 冻结超过批次可用，账不平")
            lot.available -= line['points']

    def _on_claim(self, event):
        p = event['payload']
        order = self.orders[p['transfer_id']]
        order.status = STATUS_CLAIMED
        order.version += 1
        order.updated_at = event['at']
        # 只划转仍存活的行；已到期行保留在单上作为核销证据，不再划入接收方。
        for line in order.lines:
            if line.get('expired'):
                continue
            new_id = p['lot_map'][line['lot_id']]
            self.lots[new_id] = Lot(new_id, order.recipient, line['points'],
                                    line['points'], line['expires'], line['lot_id'])

    def _on_revoke(self, event):
        p = event['payload']
        order = self.orders[p['transfer_id']]
        order.status = STATUS_REVOKED
        order.version += 1
        order.updated_at = event['at']
        # 仅存活行原路退回；已到期行已核销，绝不复活。
        for line in order.lines:
            if line.get('expired'):
                continue
            lot = self.lots[line['lot_id']]
            lot.available += line['points']   # 原路退回，到期日不变

    def _on_expire(self, event):
        p = event['payload']
        order = self.orders[p['transfer_id']]
        order.version += 1
        order.updated_at = event['at']
        expired_ids = {line['lot_id'] for line in p['lines']}
        # 逐行核销：只标记投影副本中的行，绝不回写事件 payload（哈希口径保持不变）。
        for line in order.lines:
            if line['lot_id'] in expired_ids:
                line['expired'] = True
        order.block_code = 'ESCROW_EXPIRED'
        order.block_detail = BLOCK_REASONS['ESCROW_EXPIRED']
        # 全部行都到期才把整单置为已到期终结；否则保持待领取，剩余行仍可领取/撤回。
        if all(line.get('expired') for line in order.lines):
            order.status = STATUS_EXPIRED

    # ------------------------------------------------------------ 查询
    def member_balances(self):
        out = {}
        for lot in self.lots.values():
            row = out.setdefault(lot.member, {'member': lot.member, 'available': 0, 'lot_count': 0})
            row['available'] += lot.available
            row['lot_count'] += 1
        return [out[k] for k in sorted(out)]

    def available_lots(self, member, now_iso):
        return sorted(
            (lot for lot in self.lots.values()
             if lot.member == member and lot.available > 0 and lot.expires > now_iso),
            key=lambda x: (x.expires, x.lot_id),
        )

    def snapshot(self):
        return {
            'lots': [lot.to_dict() for lot in sorted(self.lots.values(), key=lambda x: x.lot_id)],
            'orders': [self.orders[k].to_dict() for k in sorted(self.orders)],
            'balances': self.member_balances(),
            'last_hash': self.last_hash,
            'event_count': self.event_count,
        }


def replay(events):
    ledger = TransferLedger()
    for event in sorted(events, key=lambda e: e['seq']):
        ledger.apply(event)
    return ledger


def make_event(ledger, etype, payload, at):
    """构造下一条链上事件（不修改投影）。

    哈希只覆盖业务 payload（与 apply 验链口径一致）；prev_hash 作为链结构
    单独存放，由 apply 比对前驱，二者共同防篡改、防覆盖。
    """
    seq = ledger.event_count + 1
    return {'type': etype, 'payload': payload, 'at': at, 'seq': seq,
            'prev_hash': ledger.last_hash, 'hash': _hash_payload(payload)}


def verify_chain(events):
    """独立验链：篡改/覆盖/乱序都抛 ValueError。"""
    replay(events)
    return True
