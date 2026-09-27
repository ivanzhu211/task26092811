"""数据模型与枚举定义。"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

DEFAULT_DEADLINE_HOURS = 72
MIN_DEADLINE_HOURS = 1
MAX_DEADLINE_HOURS = 168
MAX_OCCUPYING_INVITATIONS = 25
IDEMPOTENCY_TTL_HOURS = 24


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    VOID = "void"


class CampaignStatus(str, enum.Enum):
    OPEN = "open"
    INVITED = "invited"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Invitation:
    id: int
    campaign_id: int
    influencer_id: str
    brand_id: str
    status: InvitationStatus
    sent_at: datetime
    deadline: datetime
    resolved_at: Optional[datetime]
    idempotency_key: str


@dataclass(frozen=True)
class Campaign:
    id: int
    brand_id: str
    deadline_hours: int
    created_at: datetime
    cancelled: bool


@dataclass(frozen=True)
class CampaignState:
    campaign: Campaign
    status: CampaignStatus
    occupying_count: int


@dataclass(frozen=True)
class SendResult:
    ok: bool
    invitation_id: Optional[int] = None
    reason: Optional[str] = None
    replayed: bool = False
