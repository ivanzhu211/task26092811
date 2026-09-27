"""异常类型定义。"""


class LedgerError(Exception):
    """账本模块所有业务异常的基类。"""


class CampaignNotFoundError(LedgerError):
    """活动不存在。"""


class CampaignCancelledError(LedgerError):
    """活动已取消，之后该活动上的操作一律失败。"""


class InvitationNotFoundError(LedgerError):
    """邀请不存在。"""


class InvitationNotPendingError(LedgerError):
    """邀请不是待回复状态（已接受/已拒绝/已过期/已撤回/已作废）。"""


class QuotaExceededError(LedgerError):
    """活动占名额的邀请已达上限。"""


class DuplicateActiveInvitationError(LedgerError):
    """同一达人在该活动已有一个占名额的邀请。"""


class InfluencerRejectedBeforeError(LedgerError):
    """该达人已拒绝过该活动，不能再被邀请。"""


class InvalidDeadlineHoursError(LedgerError):
    """接受时限必须是 1 到 168 之间的整数小时。"""

