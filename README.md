# task26092811

## invite_ledger：推广活动邀请账本库

品牌端与达人端两个后台服务以库的形式直接引用，无接口层；数据存于本地 SQLite 文件（WAL 模式），多进程通过 `BEGIN IMMEDIATE` 事务串行化写入。

### 用法

```python
from invite_ledger import InvitationLedger

ledger = InvitationLedger("/path/to/ledger.db")   # 每个进程各自打开

cid = ledger.create_campaign("brandA", now=ts, accept_ttl_hours=72).value
inv = ledger.send_invitation("brandA", cid, "talent1", "idem-key-1", now=ts)
ledger.accept_invitation("talent1", inv.value, now=ts)
ledger.withdraw_invitation("brandA", inv.value, now=ts)
ledger.cancel_campaign("brandA", cid, now=ts)
view = ledger.get_campaign(cid, now=ts).value      # 状态 / 占名额数
invs = ledger.list_invitations(cid, now=ts)
```

- 所有操作的 `now` 由调用方提供（Unix 秒），过期判断只依赖它。
- 所有操作返回 `Result`：`ok` + `value`，或 `error`（`ErrorCode`）。
- 邀请截止时刻 = 发出时刻 + 时限；到达截止时刻即过期，`expired_at` 记为截止时刻。
- 占名额 = 待回复（未过期）+ 已接受，上限 25；活动状态由邀请状态实时推导。
- 幂等键按品牌隔离，24 小时内同键同参数重放返回首次结果（含被拒绝的结果），换参数直接拒绝。

### 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_rules.py`：业务规则（时限、过期、名额、状态机、幂等、重开持久化）。
- `tests/test_concurrency.py`：多进程（spawn）同时操作同一数据文件——抢最后一个名额、同幂等键并发、接受/拒绝与撤回/取消并发；每轮结束后直接从库文件审计：占名额 ≤ 25、幂等键唯一对应邀请、活动状态与邀请状态重算一致。
