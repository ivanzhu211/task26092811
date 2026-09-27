# task26092811

## 推广活动邀请账本（invite_ledger）

纯 Python 库，无接口层，品牌端与达人端两个后台服务（独立进程）直接 import 使用。
数据持久化在单个本地 SQLite 文件中，多进程通过 WAL 模式 + `BEGIN IMMEDIATE`
事务串行化写入，保证并发下不出现半截状态。

### 用法

```python
from datetime import datetime
from invite_ledger import InviteLedger

ledger = InviteLedger("/path/to/ledger.db")   # 两个进程各自打开同一文件

now = datetime.now()                          # 当前时间一律由调用方给出
campaign = ledger.create_campaign("brandA", now, deadline_hours=72)  # 1~168，默认 72

# 品牌端
result = ledger.send_invitation(campaign.id, "influencer1", "idempotency-key-1", now)
if result.ok:
    invitation_id = result.invitation_id
ledger.withdraw(invitation_id, now)           # 撤回待回复的邀请
ledger.cancel_campaign(campaign.id, now)      # 取消活动，待回复/已接受全部作废

# 达人端
ledger.accept(invitation_id, now)
ledger.reject(invitation_id, now)

# 查询（过期与活动状态随传入的 now 实时推导，不依赖清理任务）
state = ledger.get_campaign_state(campaign.id, now)   # open/invited/accepted/cancelled
inv = ledger.get_invitation(invitation_id, now)
invs = ledger.list_invitations(campaign.id, now)
ledger.close()
```

### 规则要点

- 只有早于截止时刻（发出时刻 + 时限）才能接受/拒绝；到期即过期，过期时间记为截止时刻。
- 每活动最多 25 个占名额邀请（待回复 + 已接受）；拒绝/过期/撤回/作废不占。
- 同一达人在同一活动最多一个占名额邀请；拒绝过的达人不可再邀请，过期/撤回可再邀请。
- 幂等键按品牌区分：24 小时内同键同活动同达人重放首次结果（含被拒绝的结果）；
  同键换活动或达人返回 `idempotency_conflict`；超过 24 小时当新键。
- 业务拒绝通过 `SendResult(ok=False, reason=...)` 返回；
  非法操作（如接受已过期邀请、操作已取消活动）抛出 `invite_ledger.LedgerError` 子类。

### 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_ledger.py`：单进程业务规则（时限、名额、状态机、幂等、持久化）。
- `tests/test_concurrency.py`：真实多进程（spawn）同时操作同一数据文件——
  抢最后一个名额、同键并发、接受/拒绝与撤回/取消并发；
  每个用例结束后独立连接重算核对：名额 ≤ 25、幂等键唯一、
  活动状态与邀请状态按规则一致。
