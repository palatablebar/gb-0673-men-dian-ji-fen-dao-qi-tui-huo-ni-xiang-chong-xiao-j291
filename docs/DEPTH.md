# 深度登记 门店积分到期与退货逆向冲销

规模：中型。使用者：连锁零售会员运营专员。核心规则：消费先用最早到期批次；退款按原消费分配逆向恢复，已过期部分不得复活。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 事件采信 | 执行并复核事件采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 权益规则 | 执行并复核权益规则 | `POST /api/policy` | `app/modules/policy/rules.py` | `tests/test_policy.py` |
| M3 积分归批 | 执行并复核积分归批 | `POST /api/lots` | `app/modules/lots/rules.py` | `tests/test_lots.py` |
| M4 消费到期 | 执行并复核消费到期 | `POST /api/consumption` | `app/modules/consumption/rules.py` | `tests/test_consumption.py` |
| M5 退货回拨 | 执行并复核退货回拨 | `POST /api/refund` | `app/modules/refund/rules.py` | `tests/test_refund.py` |
| M6 余额对账 | 执行并复核余额对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 期末归档 | 执行并复核期末归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 积分转赠 | 提交托管、领取、撤回、到期清扫 | `/transfer`、`POST /api/transfer/open|claim|revoke|sweep` | `app/modules/transfer/`（domain/service/handler/seeder）、`app/transfer_store.py` | `tests/test_transfer_domain.py`、`tests/test_transfer_api.py` |

## 转赠陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-T01 | 一份积分两个可用主人 | 提交即入接收方账，转出方仍可撤回造成双花 | 提交即冻结到托管，双方可用额均不含该份，领取才划转 | `service.py::open_transfer/claim` | `test_transfer_domain.py::test_escrow_freezes_and_single_owner` |
| TRAP-T02 | 重试新增额度 | 每次提交新建单子重复冻结 | transfer_id 幂等键，同号重试命中返回不新增事件 | `service.py::open_transfer` | `test_retry_same_transfer_is_idempotent`、`test_retry_is_idempotent_no_new_credit` |
| TRAP-T03 | 并发切换覆盖余额 | 后写覆盖先写，余额凭空增减 | 链尾哈希乐观锁，冲突 409 整笔不写，旧版余额保留 | `transfer_store.py::commit` | `test_optimistic_concurrency_keeps_old_balance`、`test_concurrent_conflict_returns_409_and_preserves_balance` |
| TRAP-T04 | 历史被覆盖失去争议依据 | 状态就地 UPDATE，旧值不可追溯 | 事件只追加 + 哈希链，篡改/覆盖验链即失败，无 UPDATE/DELETE 路径 | `domain.py::apply/verify_chain`、`transfer_store.py` | `test_hash_chain_detects_tamper_and_rewrite`、`test_events_are_append_only_no_update_path` |
| TRAP-T05 | 转赠后到期承诺被重置 | 领取按新批次给新到期日 | lines 逐批保留原到期日，到账批次 source_lot 可溯，撤回原路退回 | `domain.py::_on_claim/_on_revoke` | `test_claim_transfers_with_original_expiry`、`test_revoke_returns_to_original_lots` |
| TRAP-T06 | 到期积分被领取/撤回复活 | 不校验到期日照常划转 | 领取/撤回时刻比对原到期日，阻断并 EXPIRE 核销 | `service.py::claim/revoke/sweep_expired` | `test_expired_escrow_blocks_claim_and_does_not_revive`、`test_expired_escrow_claim_blocked_and_sweep` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030权益中文 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_gb18030_real` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-08 | 会员前导零 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_member_leading_zero` |
| TRAP-04 | 重复表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header` |
| TRAP-05 | 缺列记录 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_bad` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict` |
| TRAP-18 | 空态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_three` |
| TRAP-07 | 哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_sentinel_real` |
| TRAP-11 | UTC归一 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_utc_real` |
| TRAP-06 | 全角积分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_fullwidth_points` |
| TRAP-23 | 权益半开窗 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_rule_end_exclusive` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。
