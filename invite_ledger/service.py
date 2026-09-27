"""Invitation ledger service. Used directly as a library by brand and talent services."""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from .errors import ErrorCode
from .models import CampaignStatus, CampaignView, Invitation, InvitationStatus, Result
from .storage import connect, transaction

MAX_OCCUPIED_SLOTS = 25
MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 168
DEFAULT_TTL_HOURS = 72
IDEMPOTENCY_TTL_SECONDS = 24 * 3600


class InvitationLedger:
    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _expire_overdue(conn: sqlite3.Connection, now: float, campaign_id: Optional[int] = None) -> None:
        sql = (
            "UPDATE invitations SET status = ?, expired_at = deadline "
            "WHERE status = ? AND deadline <= ?"
        )
        params: list = [InvitationStatus.EXPIRED.value, InvitationStatus.PENDING.value, now]
        if campaign_id is not None:
            sql += " AND campaign_id = ?"
            params.append(campaign_id)
        conn.execute(sql, params)

    @staticmethod
    def _occupied_slots(conn: sqlite3.Connection, campaign_id: int, now: float) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM invitations WHERE campaign_id = ? AND ("
            "  status = ? OR (status = ? AND deadline > ?)"
            ")",
            (
                campaign_id,
                InvitationStatus.ACCEPTED.value,
                InvitationStatus.PENDING.value,
                now,
            ),
        ).fetchone()
        return int(row["n"])

    def _get_campaign_row(self, conn: sqlite3.Connection, campaign_id: int):
        return conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()

    def create_campaign(
        self, brand_id: str, now: float, accept_ttl_hours: int = DEFAULT_TTL_HOURS
    ) -> Result[int]:
        if (
            not isinstance(accept_ttl_hours, int)
            or accept_ttl_hours < MIN_TTL_HOURS
            or accept_ttl_hours > MAX_TTL_HOURS
        ):
            return Result.failure(ErrorCode.INVALID_TTL)
        with transaction(self._conn) as conn:
            cur = conn.execute(
                "INSERT INTO campaigns(brand_id, accept_ttl_hours, created_at) VALUES (?, ?, ?)",
                (brand_id, accept_ttl_hours, now),
            )
            return Result.success(int(cur.lastrowid))

    def cancel_campaign(self, brand_id: str, campaign_id: int, now: float) -> Result[int]:
        with transaction(self._conn) as conn:
            campaign = self._get_campaign_row(conn, campaign_id)
            if campaign is None:
                return Result.failure(ErrorCode.CAMPAIGN_NOT_FOUND)
            if campaign["brand_id"] != brand_id:
                return Result.failure(ErrorCode.FORBIDDEN)
            if campaign["cancelled"]:
                return Result.failure(ErrorCode.CAMPAIGN_CANCELLED)
            self._expire_overdue(conn, now, campaign_id)
            conn.execute(
                "UPDATE invitations SET status = ? WHERE campaign_id = ? AND status IN (?, ?)",
                (
                    InvitationStatus.VOIDED.value,
                    campaign_id,
                    InvitationStatus.PENDING.value,
                    InvitationStatus.ACCEPTED.value,
                ),
            )
            conn.execute(
                "UPDATE campaigns SET cancelled = 1 WHERE id = ?", (campaign_id,)
            )
            return Result.success(campaign_id)

    def send_invitation(
        self,
        brand_id: str,
        campaign_id: int,
        talent_id: str,
        idempotency_key: str,
        now: float,
    ) -> Result[int]:
        with transaction(self._conn) as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency_keys WHERE brand_id = ? AND key = ?",
                (brand_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if now - existing["first_used_at"] < IDEMPOTENCY_TTL_SECONDS:
                    if (
                        existing["campaign_id"] != campaign_id
                        or existing["talent_id"] != talent_id
                    ):
                        return Result.failure(ErrorCode.IDEMPOTENCY_CONFLICT)
                    if existing["result_ok"]:
                        return Result.success(int(existing["result_invitation_id"]))
                    return Result.failure(ErrorCode(existing["result_error"]))
                conn.execute(
                    "DELETE FROM idempotency_keys WHERE brand_id = ? AND key = ?",
                    (brand_id, idempotency_key),
                )

            result = self._send_invitation_locked(conn, brand_id, campaign_id, talent_id, now)
            conn.execute(
                "INSERT INTO idempotency_keys("
                "  brand_id, key, campaign_id, talent_id, first_used_at,"
                "  result_ok, result_invitation_id, result_error"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brand_id,
                    idempotency_key,
                    campaign_id,
                    talent_id,
                    now,
                    1 if result.ok else 0,
                    result.value if result.ok else None,
                    None if result.ok else result.error.value,
                ),
            )
            return result

    def _send_invitation_locked(
        self,
        conn: sqlite3.Connection,
        brand_id: str,
        campaign_id: int,
        talent_id: str,
        now: float,
    ) -> Result[int]:
        campaign = self._get_campaign_row(conn, campaign_id)
        if campaign is None:
            return Result.failure(ErrorCode.CAMPAIGN_NOT_FOUND)
        if campaign["brand_id"] != brand_id:
            return Result.failure(ErrorCode.FORBIDDEN)
        if campaign["cancelled"]:
            return Result.failure(ErrorCode.CAMPAIGN_CANCELLED)

        self._expire_overdue(conn, now, campaign_id)

        declined = conn.execute(
            "SELECT 1 FROM invitations WHERE campaign_id = ? AND talent_id = ? AND status = ?",
            (campaign_id, talent_id, InvitationStatus.DECLINED.value),
        ).fetchone()
        if declined is not None:
            return Result.failure(ErrorCode.TALENT_DECLINED)

        occupied_talent = conn.execute(
            "SELECT 1 FROM invitations WHERE campaign_id = ? AND talent_id = ? AND ("
            "  status = ? OR (status = ? AND deadline > ?)"
            ")",
            (
                campaign_id,
                talent_id,
                InvitationStatus.ACCEPTED.value,
                InvitationStatus.PENDING.value,
                now,
            ),
        ).fetchone()
        if occupied_talent is not None:
            return Result.failure(ErrorCode.TALENT_OCCUPIED)

        if self._occupied_slots(conn, campaign_id, now) >= MAX_OCCUPIED_SLOTS:
            return Result.failure(ErrorCode.CAPACITY_FULL)

        deadline = now + int(campaign["accept_ttl_hours"]) * 3600
        cur = conn.execute(
            "INSERT INTO invitations("
            "  campaign_id, brand_id, talent_id, status, sent_at, deadline"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                campaign_id,
                brand_id,
                talent_id,
                InvitationStatus.PENDING.value,
                now,
                deadline,
            ),
        )
        return Result.success(int(cur.lastrowid))

    def _respond_invitation(
        self,
        talent_id: str,
        invitation_id: int,
        now: float,
        new_status: InvitationStatus,
    ) -> Result[int]:
        with transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            if row is None:
                return Result.failure(ErrorCode.INVITATION_NOT_FOUND)
            if row["talent_id"] != talent_id:
                return Result.failure(ErrorCode.FORBIDDEN)
            if row["status"] != InvitationStatus.PENDING.value or row["deadline"] <= now:
                return Result.failure(ErrorCode.INVITATION_NOT_PENDING)
            conn.execute(
                "UPDATE invitations SET status = ?, responded_at = ? WHERE id = ?",
                (new_status.value, now, invitation_id),
            )
            return Result.success(invitation_id)

    def accept_invitation(self, talent_id: str, invitation_id: int, now: float) -> Result[int]:
        return self._respond_invitation(talent_id, invitation_id, now, InvitationStatus.ACCEPTED)

    def decline_invitation(self, talent_id: str, invitation_id: int, now: float) -> Result[int]:
        return self._respond_invitation(talent_id, invitation_id, now, InvitationStatus.DECLINED)

    def withdraw_invitation(self, brand_id: str, invitation_id: int, now: float) -> Result[int]:
        with transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            if row is None:
                return Result.failure(ErrorCode.INVITATION_NOT_FOUND)
            if row["brand_id"] != brand_id:
                return Result.failure(ErrorCode.FORBIDDEN)
            if row["status"] != InvitationStatus.PENDING.value or row["deadline"] <= now:
                return Result.failure(ErrorCode.INVITATION_NOT_PENDING)
            conn.execute(
                "UPDATE invitations SET status = ?, responded_at = ? WHERE id = ?",
                (InvitationStatus.WITHDRAWN.value, now, invitation_id),
            )
            return Result.success(invitation_id)

    def get_campaign(self, campaign_id: int, now: float) -> Result[CampaignView]:
        with transaction(self._conn) as conn:
            campaign = self._get_campaign_row(conn, campaign_id)
            if campaign is None:
                return Result.failure(ErrorCode.CAMPAIGN_NOT_FOUND)
            self._expire_overdue(conn, now, campaign_id)
            occupied = self._occupied_slots(conn, campaign_id, now)
            if campaign["cancelled"]:
                status = CampaignStatus.CANCELLED
            else:
                accepted = conn.execute(
                    "SELECT 1 FROM invitations WHERE campaign_id = ? AND status = ?",
                    (campaign_id, InvitationStatus.ACCEPTED.value),
                ).fetchone()
                if accepted is not None:
                    status = CampaignStatus.ACCEPTED
                elif occupied > 0:
                    status = CampaignStatus.INVITED
                else:
                    status = CampaignStatus.OPEN
            return Result.success(
                CampaignView(
                    id=int(campaign["id"]),
                    brand_id=campaign["brand_id"],
                    accept_ttl_hours=int(campaign["accept_ttl_hours"]),
                    created_at=float(campaign["created_at"]),
                    cancelled=bool(campaign["cancelled"]),
                    status=status,
                    occupied_slots=occupied,
                )
            )

    def list_invitations(self, campaign_id: int, now: float) -> List[Invitation]:
        with transaction(self._conn) as conn:
            self._expire_overdue(conn, now, campaign_id)
            rows = conn.execute(
                "SELECT * FROM invitations WHERE campaign_id = ? ORDER BY id",
                (campaign_id,),
            ).fetchall()
            return [
                Invitation(
                    id=int(row["id"]),
                    campaign_id=int(row["campaign_id"]),
                    brand_id=row["brand_id"],
                    talent_id=row["talent_id"],
                    status=InvitationStatus(row["status"]),
                    sent_at=float(row["sent_at"]),
                    deadline=float(row["deadline"]),
                    responded_at=(
                        None if row["responded_at"] is None else float(row["responded_at"])
                    ),
                    expired_at=(
                        None if row["expired_at"] is None else float(row["expired_at"])
                    ),
                )
                for row in rows
            ]
