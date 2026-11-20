"""转赠事件存储：只追加事件日志，哈希链尾值做乐观并发控制。

历史只追加：没有 UPDATE/DELETE 路径；覆盖历史会在领域层验链时直接报错，
争议依据不可被静默抹掉。
"""
import json
import sqlite3
from pathlib import Path
from app.errors import BusinessError


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS transfer_events (
        seq INTEGER PRIMARY KEY,
        transfer_id TEXT NOT NULL,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        hash TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        at TEXT NOT NULL
    )''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_transfer_events_order ON transfer_events(transfer_id)')
    return con


class SqliteTransferRepository:
    def __init__(self, path):
        self.path = path

    def load_events(self):
        with connect(self.path) as con:
            rows = con.execute('SELECT * FROM transfer_events ORDER BY seq').fetchall()
        return [{'seq': r['seq'], 'type': r['type'], 'payload': json.loads(r['payload']),
                 'hash': r['hash'], 'prev_hash': r['prev_hash'], 'at': r['at']}
                for r in rows]

    def commit(self, events, expected_tail=None):
        """整笔提交：库尾必须等于读取时链尾（即首条事件的 prev_hash），否则 409。

        并发判定只信首事件记录的前驱哈希；expected_tail 为可选的调用方自检入参，
        单事件追加时与首事件 prev_hash 相同。冲突时整笔不写，旧版余额原样保留。
        """
        if not events:
            return
        required_tail = events[0]['prev_hash']
        with connect(self.path) as con:
            row = con.execute('SELECT hash FROM transfer_events ORDER BY seq DESC LIMIT 1').fetchone()
            current_tail = row['hash'] if row else '0' * 64
            if current_tail != required_tail:
                raise BusinessError('STALE_VERSION',
                                    '版本已变化，请刷新后重试（旧版余额未改动）', 409)
            for event in events:
                transfer_id = event['payload'].get('transfer_id',
                                                    event['payload'].get('lot_id', ''))
                con.execute('INSERT INTO transfer_events(seq,transfer_id,type,payload,hash,prev_hash,at)'
                            ' VALUES(?,?,?,?,?,?,?)',
                            (event['seq'], transfer_id, event['type'],
                             json.dumps(event['payload'], ensure_ascii=False),
                             event['hash'], event['prev_hash'], event['at']))

    def seeded(self):
        with connect(self.path) as con:
            return con.execute('SELECT 1 FROM transfer_events LIMIT 1').fetchone() is not None
