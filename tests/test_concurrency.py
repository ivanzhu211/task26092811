"""Multi-process concurrency tests: real processes, one shared SQLite file."""

import multiprocessing as mp
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from invite_ledger import CampaignStatus, InvitationLedger, InvitationStatus  # noqa: E402
from concurrency_workers import (  # noqa: E402
    worker_accept,
    worker_cancel,
    worker_decline,
    worker_send,
    worker_withdraw,
)

HOUR = 3600.0
NOW = 1_000_000.0


def run_race(db_path, workers):
    """Start all workers behind a barrier, collect one result per worker."""
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(workers))
    queues = [ctx.Queue() for _ in workers]
    procs = [
        ctx.Process(target=target, args=(db_path, barrier, queues[i], *args))
        for i, (target, args) in enumerate(workers)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode}"
    return [queue.get(timeout=10) for queue in queues]


def audit_ledger(db_path, now):
    """Cross-check final state directly from the database file."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    campaigns = conn.execute("SELECT * FROM campaigns").fetchall()
    ledger = InvitationLedger(db_path)
    try:
        for campaign in campaigns:
            cid = campaign["id"]
            invitations = conn.execute(
                "SELECT * FROM invitations WHERE campaign_id = ?", (cid,)
            ).fetchall()
            occupied = sum(
                1
                for inv in invitations
                if inv["status"] == InvitationStatus.ACCEPTED.value
                or (inv["status"] == InvitationStatus.PENDING.value and inv["deadline"] > now)
            )
            assert occupied <= 25, f"campaign {cid}: {occupied} occupied slots"

            if campaign["cancelled"]:
                expected = CampaignStatus.CANCELLED
            elif any(inv["status"] == InvitationStatus.ACCEPTED.value for inv in invitations):
                expected = CampaignStatus.ACCEPTED
            elif any(
                inv["status"] == InvitationStatus.PENDING.value and inv["deadline"] > now
                for inv in invitations
            ):
                expected = CampaignStatus.INVITED
            else:
                expected = CampaignStatus.OPEN
            view = ledger.get_campaign(cid, now).value
            assert view.status == expected, f"campaign {cid}: {view.status} != {expected}"
            assert view.occupied_slots == occupied

            for inv in invitations:
                if inv["status"] == InvitationStatus.EXPIRED.value:
                    assert inv["expired_at"] == inv["deadline"]

        keys = conn.execute("SELECT * FROM idempotency_keys").fetchall()
        assert len(keys) == len({(k["brand_id"], k["key"]) for k in keys})
        for key in keys:
            if key["result_ok"]:
                row = conn.execute(
                    "SELECT * FROM invitations WHERE id = ?",
                    (key["result_invitation_id"],),
                ).fetchone()
                assert row is not None
                assert row["campaign_id"] == key["campaign_id"]
                assert row["talent_id"] == key["talent_id"]
    finally:
        ledger.close()
        conn.close()


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "ledger.db")


@pytest.fixture()
def fresh_campaign(db_path):
    ledger = InvitationLedger(db_path)
    cid = ledger.create_campaign("brandA", now=NOW).value
    ledger.close()
    return cid


def seed_invitation(db_path, campaign_id, talent="t1", key="seed", now=NOW):
    ledger = InvitationLedger(db_path)
    inv = ledger.send_invitation("brandA", campaign_id, talent, key, now).value
    ledger.close()
    return inv


def test_last_slot_race(db_path, fresh_campaign):
    cid = fresh_campaign
    ledger = InvitationLedger(db_path)
    for i in range(24):
        assert ledger.send_invitation("brandA", cid, f"seed{i}", f"seedk{i}", NOW).ok
    ledger.close()

    results = run_race(
        db_path,
        [(worker_send, ("brandA", cid, f"racer{i}", f"racek{i}", NOW)) for i in range(8)],
    )
    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    assert len(winners) == 1
    assert all(r["error"] == "CAPACITY_FULL" for r in losers)
    audit_ledger(db_path, NOW)


def test_full_capacity_race(db_path, fresh_campaign):
    cid = fresh_campaign
    results = run_race(
        db_path,
        [(worker_send, ("brandA", cid, f"t{i}", f"k{i}", NOW)) for i in range(30)],
    )
    assert sum(1 for r in results if r["ok"]) == 25
    audit_ledger(db_path, NOW)


def test_same_idempotency_key_across_processes(db_path, fresh_campaign):
    cid = fresh_campaign
    results = run_race(
        db_path,
        [(worker_send, ("brandA", cid, "t1", "shared-key", NOW)) for _ in range(4)],
    )
    assert all(r["ok"] for r in results)
    assert len({r["value"] for r in results}) == 1
    ledger = InvitationLedger(db_path)
    invitations = ledger.list_invitations(cid, NOW)
    ledger.close()
    assert len(invitations) == 1
    audit_ledger(db_path, NOW)


def test_accept_vs_withdraw(db_path, fresh_campaign):
    cid = fresh_campaign
    inv = seed_invitation(db_path, cid)
    results = run_race(
        db_path,
        [
            (worker_accept, ("t1", inv, NOW + 10)),
            (worker_withdraw, ("brandA", inv, NOW + 10)),
        ],
    )
    assert sum(1 for r in results if r["ok"]) == 1
    loser = [r for r in results if not r["ok"]][0]
    assert loser["error"] == "INVITATION_NOT_PENDING"
    ledger = InvitationLedger(db_path)
    status = ledger.list_invitations(cid, NOW + 10)[0].status
    ledger.close()
    assert status in (InvitationStatus.ACCEPTED, InvitationStatus.WITHDRAWN)
    audit_ledger(db_path, NOW + 10)


def test_decline_vs_withdraw(db_path, fresh_campaign):
    cid = fresh_campaign
    inv = seed_invitation(db_path, cid)
    results = run_race(
        db_path,
        [
            (worker_decline, ("t1", inv, NOW + 10)),
            (worker_withdraw, ("brandA", inv, NOW + 10)),
        ],
    )
    assert sum(1 for r in results if r["ok"]) == 1
    loser = [r for r in results if not r["ok"]][0]
    assert loser["error"] == "INVITATION_NOT_PENDING"
    ledger = InvitationLedger(db_path)
    status = ledger.list_invitations(cid, NOW + 10)[0].status
    ledger.close()
    assert status in (InvitationStatus.DECLINED, InvitationStatus.WITHDRAWN)
    audit_ledger(db_path, NOW + 10)


def test_accept_vs_cancel(db_path, fresh_campaign):
    cid = fresh_campaign
    inv = seed_invitation(db_path, cid)
    results = run_race(
        db_path,
        [
            (worker_accept, ("t1", inv, NOW + 10)),
            (worker_cancel, ("brandA", cid, NOW + 10)),
        ],
    )
    accept_result, cancel_result = results[0], results[1]
    assert cancel_result["ok"]
    if not accept_result["ok"]:
        assert accept_result["error"] == "INVITATION_NOT_PENDING"
    ledger = InvitationLedger(db_path)
    view = ledger.get_campaign(cid, NOW + 10).value
    invitation = ledger.list_invitations(cid, NOW + 10)[0]
    ledger.close()
    assert view.status == CampaignStatus.CANCELLED
    assert invitation.status == InvitationStatus.VOIDED
    audit_ledger(db_path, NOW + 10)
