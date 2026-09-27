"""Shared types for the invitation ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Optional, TypeVar

from .errors import ErrorCode


class InvitationStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"
    VOIDED = "VOIDED"


class CampaignStatus(str, Enum):
    OPEN = "OPEN"
    INVITED = "INVITED"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"


T = TypeVar("T")


@dataclass(frozen=True)
class Result(Generic[T]):
    """Outcome of a ledger operation. Either ok with a value, or an error code."""

    ok: bool
    value: Optional[T] = None
    error: Optional[ErrorCode] = None

    @staticmethod
    def success(value: T) -> "Result[T]":
        return Result(ok=True, value=value)

    @staticmethod
    def failure(error: ErrorCode) -> "Result[T]":
        return Result(ok=False, error=error)


@dataclass(frozen=True)
class Invitation:
    id: int
    campaign_id: int
    brand_id: str
    talent_id: str
    status: InvitationStatus
    sent_at: float
    deadline: float
    responded_at: Optional[float] = None
    expired_at: Optional[float] = None


@dataclass(frozen=True)
class CampaignView:
    id: int
    brand_id: str
    accept_ttl_hours: int
    created_at: float
    cancelled: bool
    status: CampaignStatus
    occupied_slots: int
