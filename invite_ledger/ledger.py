"""邀请账本主入口：品牌端与达人端后台服务直接调用本模块。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Union

from . import db
from .errors import (
    CampaignCancelledError,
    CampaignNotFoundError,
    InvalidDeadlineHoursError,
    InvitationNotFoundError,
    InvitationNotPendingError,
)
from .models import (
    DEFAULT_DEADLINE_HOURS,
    IDEMPOTENCY_TTL_HOURS,
    MAX_DEADLINE_HOURS,
    MAX_OCCUPYING_INVITATIONS,
    MIN_DEADLINE_HOURS,
    Campaign,
    CampaignState,
    CampaignStatus,
    Invitation,
    InvitationStatus,
    SendResult,
)


def _to_iso(value: datetime) -> str:
    return value.isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_campaign(row) -> Campaign:
    return Campaign(
        id=row["id"],
        brand_id=row["brand_id"],
        deadline_hours=row["deadline_hours"],
        created_at=_from_iso(row["created_at"]),
        cancelled=bool(row["cancelled"]),
    )


def _row_to_invitation(row, now: datetime) -> Invitation:
    status = InvitationStatus(row["status"])
    deadline = _from_iso(row["deadline"])
    resolved_at = _from_iso(row["resolved_at"]) if row["resolved_at"] else None
    if status is InvitationStatus.PENDING and deadline <= now:
        status = InvitationStatus.EXPIRED
        resolved_at = deadline
    return Invitation(
        id=row["id"],
        campaign_id=row["campaign_id"],
        influencer_id=row["influencer_id"],
        brand_id=row["brand_id"],
        status=status,
        sent_at=_from_iso(row["sent_at"]),
        deadline=deadline,
        resolved_at=resolved_at,
        idempotency_key=row["idempotency_key"],
    )


class InviteLedger:
    """推广活动邀请账本。所有时间判断以调用方传入的 now 为准。"""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self._conn = db.connect(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "InviteLedger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _load_campaign_row(self, conn, campaign_id: int):
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise CampaignNotFoundError(f"活动不存在: {campaign_id}")
        return row

    @staticmethod
    def _occupying_counts(conn, campaign_id: int, now: datetime):
        """返回 (待回复未过期数, 已接受数)。过期只看当前时间，不依赖清理。"""
        accepted = conn.execute(
            "SELECT COUNT(*) AS c FROM invitations "
            "WHERE campaign_id = ? AND status = ?",
            (campaign_id, InvitationStatus.ACCEPTED.value),
        ).fetchone()["c"]
        pending = 0
        rows = conn.execute(
            "SELECT deadline FROM invitations "
            "WHERE campaign_id = ? AND status = ?",
            (campaign_id, InvitationStatus.PENDING.value),
        ).fetchall()
        for row in rows:
            if _from_iso(row["deadline"]) > now:
                pending += 1
        return pending, accepted

    def create_campaign(
        self, brand_id: str, now: datetime, deadline_hours: Optional[int] = None
    ) -> Campaign:
        hours = DEFAULT_DEADLINE_HOURS if deadline_hours is None else deadline_hours
        if (
            isinstance(hours, bool)
            or not isinstance(hours, int)
            or not (MIN_DEADLINE_HOURS <= hours <= MAX_DEADLINE_HOURS)
        ):
            raise InvalidDeadlineHoursError(
                f"接受时限必须是 {MIN_DEADLINE_HOURS} 到 {MAX_DEADLINE_HOURS} "
                f"之间的整数小时: {deadline_hours!r}"
            )
        with db.immediate_transaction(self._conn) as conn:
            cur = conn.execute(
                "INSERT INTO campaigns (brand_id, deadline_hours, created_at) "
                "VALUES (?, ?, ?)",
                (brand_id, hours, _to_iso(now)),
            )
            row = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_campaign(row)

    def cancel_campaign(self, campaign_id: int, now: datetime) -> None:
        with db.immediate_transaction(self._conn) as conn:
            row = self._load_campaign_row(conn, campaign_id)
            if row["cancelled"]:
                raise CampaignCancelledError(f"活动已取消: {campaign_id}")
            conn.execute(
                "UPDATE campaigns SET cancelled = 1 WHERE id = ?", (campaign_id,)
            )
            # 待回复（未过期）与已接受的邀请全部作废；已过期的保持过期。
            pending_rows = conn.execute(
                "SELECT id, deadline FROM invitations "
                "WHERE campaign_id = ? AND status = ?",
                (campaign_id, InvitationStatus.PENDING.value),
            ).fetchall()
            void_ids = [
                r["id"]
                for r in pending_rows
                if _from_iso(r["deadline"]) > now
            ]
            accepted_rows = conn.execute(
                "SELECT id FROM invitations "
                "WHERE campaign_id = ? AND status = ?",
                (campaign_id, InvitationStatus.ACCEPTED.value),
            ).fetchall()
            void_ids += [r["id"] for r in accepted_rows]
            for inv_id in void_ids:
                conn.execute(
                    "UPDATE invitations SET status = ?, resolved_at = ? "
                    "WHERE id = ?",
                    (InvitationStatus.VOID.value, _to_iso(now), inv_id),
                )

    def send_invitation(
        self,
        campaign_id: int,
        influencer_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> SendResult:
        with db.immediate_transaction(self._conn) as conn:
            campaign_row = self._load_campaign_row(conn, campaign_id)
            brand_id = campaign_row["brand_id"]

            key_row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE brand_id = ? AND key = ?",
                (brand_id, idempotency_key),
            ).fetchone()
            if key_row is not None:
                key_age = now - _from_iso(key_row["created_at"])
                if key_age < timedelta(hours=IDEMPOTENCY_TTL_HOURS):
                    if (
                        key_row["campaign_id"] != campaign_id
                        or key_row["influencer_id"] != influencer_id
                    ):
                        return SendResult(ok=False, reason="idempotency_conflict")
                    return SendResult(
                        ok=bool(key_row["result_ok"]),
                        invitation_id=key_row["result_invitation_id"],
                        reason=key_row["result_reason"],
                        replayed=True,
                    )
                # 超过 24 小时的键当新键处理。
                conn.execute(
                    "DELETE FROM idempotency_keys WHERE brand_id = ? AND key = ?",
                    (brand_id, idempotency_key),
                )

            result = self._try_create_invitation(
                conn, campaign_row, influencer_id, idempotency_key, now
            )
            conn.execute(
                "INSERT INTO idempotency_keys "
                "(brand_id, key, campaign_id, influencer_id, result_ok, "
                " result_invitation_id, result_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brand_id,
                    idempotency_key,
                    campaign_id,
                    influencer_id,
                    int(result.ok),
                    result.invitation_id,
                    result.reason,
                    _to_iso(now),
                ),
            )
            return result

    def _try_create_invitation(
        self, conn, campaign_row, influencer_id: str, idempotency_key: str, now: datetime
    ) -> SendResult:
        campaign_id = campaign_row["id"]
        if campaign_row["cancelled"]:
            return SendResult(ok=False, reason="campaign_cancelled")

        rejected = conn.execute(
            "SELECT 1 FROM invitations WHERE campaign_id = ? AND influencer_id = ? "
            "AND status = ? LIMIT 1",
            (campaign_id, influencer_id, InvitationStatus.REJECTED.value),
        ).fetchone()
        if rejected is not None:
            return SendResult(ok=False, reason="influencer_rejected_before")

        active_rows = conn.execute(
            "SELECT status, deadline FROM invitations "
            "WHERE campaign_id = ? AND influencer_id = ? AND status IN (?, ?)",
            (
                campaign_id,
                influencer_id,
                InvitationStatus.PENDING.value,
                InvitationStatus.ACCEPTED.value,
            ),
        ).fetchall()
        for row in active_rows:
            occupying = row["status"] == InvitationStatus.ACCEPTED.value or (
                _from_iso(row["deadline"]) > now
            )
            if occupying:
                return SendResult(ok=False, reason="duplicate_active_invitation")

        pending, accepted = self._occupying_counts(conn, campaign_id, now)
        if pending + accepted >= MAX_OCCUPYING_INVITATIONS:
            return SendResult(ok=False, reason="quota_exceeded")

        deadline = now + timedelta(hours=campaign_row["deadline_hours"])
        cur = conn.execute(
            "INSERT INTO invitations "
            "(campaign_id, influencer_id, brand_id, status, sent_at, deadline, "
            " resolved_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                campaign_id,
                influencer_id,
                campaign_row["brand_id"],
                InvitationStatus.PENDING.value,
                _to_iso(now),
                _to_iso(deadline),
                idempotency_key,
            ),
        )
        return SendResult(ok=True, invitation_id=cur.lastrowid)

    def _resolve_invitation(
        self, invitation_id: int, now: datetime, target: InvitationStatus
    ) -> Invitation:
        with db.immediate_transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            if row is None:
                raise InvitationNotFoundError(f"邀请不存在: {invitation_id}")
            campaign_row = self._load_campaign_row(conn, row["campaign_id"])
            if campaign_row["cancelled"]:
                raise CampaignCancelledError(
                    f"活动已取消: {row['campaign_id']}"
                )
            if row["status"] != InvitationStatus.PENDING.value or (
                _from_iso(row["deadline"]) <= now
            ):
                raise InvitationNotPendingError(
                    f"邀请不是待回复状态: {invitation_id}"
                )
            conn.execute(
                "UPDATE invitations SET status = ?, resolved_at = ? WHERE id = ?",
                (target.value, _to_iso(now), invitation_id),
            )
            row = conn.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            return _row_to_invitation(row, now)

    def accept(self, invitation_id: int, now: datetime) -> Invitation:
        return self._resolve_invitation(
            invitation_id, now, InvitationStatus.ACCEPTED
        )

    def reject(self, invitation_id: int, now: datetime) -> Invitation:
        return self._resolve_invitation(
            invitation_id, now, InvitationStatus.REJECTED
        )

    def withdraw(self, invitation_id: int, now: datetime) -> Invitation:
        return self._resolve_invitation(
            invitation_id, now, InvitationStatus.WITHDRAWN
        )

    def get_invitation(self, invitation_id: int, now: datetime) -> Invitation:
        row = self._conn.execute(
            "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        if row is None:
            raise InvitationNotFoundError(f"邀请不存在: {invitation_id}")
        return _row_to_invitation(row, now)

    def list_invitations(self, campaign_id: int, now: datetime) -> List[Invitation]:
        rows = self._conn.execute(
            "SELECT * FROM invitations WHERE campaign_id = ? ORDER BY id",
            (campaign_id,),
        ).fetchall()
        return [_row_to_invitation(row, now) for row in rows]

    def get_campaign_state(self, campaign_id: int, now: datetime) -> CampaignState:
        row = self._conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise CampaignNotFoundError(f"活动不存在: {campaign_id}")
        campaign = _row_to_campaign(row)
        pending, accepted = self._occupying_counts(self._conn, campaign_id, now)
        if campaign.cancelled:
            status = CampaignStatus.CANCELLED
        elif accepted > 0:
            status = CampaignStatus.ACCEPTED
        elif pending > 0:
            status = CampaignStatus.INVITED
        else:
            status = CampaignStatus.OPEN
        return CampaignState(
            campaign=campaign,
            status=status,
            occupying_count=pending + accepted,
        )
