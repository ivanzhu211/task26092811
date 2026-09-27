"""推广活动邀请账本库。"""

from .errors import (
    CampaignCancelledError,
    CampaignNotFoundError,
    DuplicateActiveInvitationError,
    InfluencerRejectedBeforeError,
    InvalidDeadlineHoursError,
    InvitationNotFoundError,
    InvitationNotPendingError,
    LedgerError,
    QuotaExceededError,
)
from .ledger import InviteLedger
from .models import (
    Campaign,
    CampaignState,
    CampaignStatus,
    Invitation,
    InvitationStatus,
    SendResult,
)

__all__ = [
    "InviteLedger",
    "Campaign",
    "CampaignState",
    "CampaignStatus",
    "Invitation",
    "InvitationStatus",
    "SendResult",
    "LedgerError",
    "CampaignNotFoundError",
    "CampaignCancelledError",
    "InvitationNotFoundError",
    "InvitationNotPendingError",
    "QuotaExceededError",
    "DuplicateActiveInvitationError",
    "InfluencerRejectedBeforeError",
    "InvalidDeadlineHoursError",
]
