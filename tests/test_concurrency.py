"""多进程并发测试：多个进程同时操作同一个 SQLite 数据文件。"""

import multiprocessing as mp
import sqlite3
from datetime import datetime, timedelta

import pytest

from invite_ledger import (
    CampaignStatus,
    InvitationStatus,
    InviteLedger,
)

T0 = datetime(2026, 9, 28, 12, 0, 0)
T0_ISO = T0.isoformat()


# ---------------------------------------------------------------- workers

def _send_worker(db_path, campaign_id, influencer_id, key, barrier, queue):
    ledger = InviteLedger(db_path)
    try:
        barrier.wait(timeout=30)
        r = ledger.send_invitation(campaign_id, influencer_id, key, T0)
        queue.put(("send", r.ok, r.invitation_id, r.reason, r.replayed))
    except Exception as exc:  # noqa: BLE001
        queue.put(("exc", type(exc).__name__, str(exc)))
    finally:
        ledger.close()


def _resolve_worker(db_path, invitation_id, action, barrier, queue):
    ledger = InviteLedger(db_path)
    try:
        barrier.wait(timeout=30)
        inv = getattr(ledger, action)(invitation_id, T0)
        queue.put((action, "ok", inv.status.value))
    except Exception as exc:  # noqa: BLE001
        queue.put((action, "exc", type(exc).__name__))
    finally:
        ledger.close()


def _cancel_worker(db_path, campaign_id, barrier, queue):
    ledger = InviteLedger(db_path)
    try:
        barrier.wait(timeout=30)
        ledger.cancel_campaign(campaign_id, T0)
        queue.put(("cancel", "ok", None))
    except Exception as exc:  # noqa: BLE001
        queue.put(("cancel", "exc", type(exc).__name__))
    finally:
        ledger.close()


# ---------------------------------------------------------------- helpers

def _run(ctx, entries):
    """entries: [(target, args), ...]，同时起跑，收集所有结果。"""
    barrier = ctx.Barrier(len(entries))
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=target, args=(*args, barrier, queue))
        for target, args in entries
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"子进程异常退出: {p.exitcode}"
    results = []
    while not queue.empty():
        results.append(queue.get(timeout=10))
    assert len(results) == len(entries)
    return results


def _fresh_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = InviteLedger(path)
    return ledger, path


def _assert_consistent(db_path, campaign_id, now):
    """独立连接核对：名额、幂等键唯一性、活动状态与邀请状态重算一致。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        invitations = conn.execute(
            "SELECT * FROM invitations WHERE campaign_id = ?", (campaign_id,)
        ).fetchall()

        pending = accepted = 0
        for inv in invitations:
            status = inv["status"]
            if status == "pending" and datetime.fromisoformat(
                inv["deadline"]
            ) <= now:
                status = "expired"
            if status == "pending":
                pending += 1
            elif status == "accepted":
                accepted += 1
        occupying = pending + accepted
        assert occupying <= 25, f"占名额数超限: {occupying}"

        if campaign["cancelled"]:
            expected = CampaignStatus.CANCELLED
        elif accepted:
            expected = CampaignStatus.ACCEPTED
        elif pending:
            expected = CampaignStatus.INVITED
        else:
            expected = CampaignStatus.OPEN

        ledger = InviteLedger(db_path)
        try:
            actual = ledger.get_campaign_state(campaign_id, now)
        finally:
            ledger.close()
        assert actual.status is expected
        assert actual.occupying_count == occupying

        # 幂等键：每个键最多对应一条邀请，且成功结果指向存在的邀请。
        keys = conn.execute("SELECT * FROM idempotency_keys").fetchall()
        key_to_inv = {}
        for k in keys:
            if k["result_ok"]:
                inv_id = k["result_invitation_id"]
                assert inv_id is not None
                assert (k["brand_id"], k["key"]) not in key_to_inv
                key_to_inv[(k["brand_id"], k["key"])] = inv_id
                exists = conn.execute(
                    "SELECT 1 FROM invitations WHERE id = ?", (inv_id,)
                ).fetchone()
                assert exists is not None
        # 同一（品牌, 键）不应产生两条邀请。
        pairs = [
            (inv["brand_id"], inv["idempotency_key"]) for inv in invitations
        ]
        assert len(pairs) == len(set(pairs)), "同一幂等键产生了两条邀请"
    finally:
        conn.close()


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def ctx():
    return mp.get_context("spawn")


# ---------------------------------------------------------------- tests

class TestQuotaRace:
    def test_last_slot_race(self, tmp_path, ctx):
        """名额只差一个时，8 个进程同时发邀请，只能成功一个。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=24)
        for i in range(24):
            r = ledger.send_invitation(c.id, f"inf{i}", f"k{i}", T0)
            assert r.ok
        ledger.close()

        entries = [
            (_send_worker, (str(path), c.id, f"racer{i}", f"race-key{i}"))
            for i in range(8)
        ]
        results = _run(ctx, entries)

        wins = [r for r in results if r[0] == "send" and r[1]]
        losses = [
            r for r in results
            if r[0] == "send" and not r[1] and r[3] == "quota_exceeded"
        ]
        assert len(wins) == 1, results
        assert len(losses) == 7, results

        ledger = InviteLedger(path)
        try:
            state = ledger.get_campaign_state(c.id, T0)
            assert state.occupying_count == 25
        finally:
            ledger.close()
        _assert_consistent(path, c.id, T0)


class TestIdempotencyRace:
    def test_same_key_many_processes(self, tmp_path, ctx):
        """6 个进程同时用同一个键发同一邀请：只有一条邀请，结果一致。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=24)
        ledger.close()

        entries = [
            (_send_worker, (str(path), c.id, "inf1", "shared-key"))
            for _ in range(6)
        ]
        results = _run(ctx, entries)

        oks = [r for r in results if r[0] == "send" and r[1]]
        assert len(oks) == 6, results
        inv_ids = {r[2] for r in oks}
        assert len(inv_ids) == 1, results
        assert sum(1 for r in oks if not r[4]) == 1  # 恰好一次非重放

        ledger = InviteLedger(path)
        try:
            assert len(ledger.list_invitations(c.id, T0)) == 1
            assert ledger.get_campaign_state(c.id, T0).occupying_count == 1
        finally:
            ledger.close()
        _assert_consistent(path, c.id, T0)

    def test_same_key_conflict_race(self, tmp_path, ctx):
        """两个进程用同一个键发不同达人：一个成功，一个冲突拒绝。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=24)
        ledger.close()

        entries = [
            (_send_worker, (str(path), c.id, "inf1", "shared-key")),
            (_send_worker, (str(path), c.id, "inf2", "shared-key")),
        ]
        results = _run(ctx, entries)

        oks = [r for r in results if r[0] == "send" and r[1]]
        conflicts = [
            r for r in results
            if r[0] == "send" and not r[1] and r[3] == "idempotency_conflict"
        ]
        assert len(oks) == 1, results
        assert len(conflicts) == 1, results

        ledger = InviteLedger(path)
        try:
            assert len(ledger.list_invitations(c.id, T0)) == 1
        finally:
            ledger.close()
        _assert_consistent(path, c.id, T0)


class TestResolveRaces:
    @pytest.mark.parametrize(
        "action_a,action_b",
        [("accept", "withdraw"), ("reject", "withdraw")],
    )
    def test_resolve_vs_withdraw(self, tmp_path, ctx, action_a, action_b):
        """接受/拒绝 与 撤回 并发：只能一方生效，另一方明确失败。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=24)
        r = ledger.send_invitation(c.id, "inf1", "k1", T0)
        inv_id = r.invitation_id
        ledger.close()

        entries = [
            (_resolve_worker, (str(path), inv_id, action_a)),
            (_resolve_worker, (str(path), inv_id, action_b)),
        ]
        results = _run(ctx, entries)

        oks = [r for r in results if r[1] == "ok"]
        fails = [
            r for r in results
            if r[1] == "exc" and r[2] == "InvitationNotPendingError"
        ]
        assert len(oks) == 1, results
        assert len(fails) == 1, results

        expected = {
            "accept": InvitationStatus.ACCEPTED,
            "reject": InvitationStatus.REJECTED,
            "withdraw": InvitationStatus.WITHDRAWN,
        }
        winner = oks[0][0]
        ledger = InviteLedger(path)
        try:
            final = ledger.get_invitation(inv_id, T0)
            assert final.status is expected[winner]
        finally:
            ledger.close()
        _assert_consistent(path, c.id, T0)

    @pytest.mark.parametrize("action", ["accept", "reject"])
    def test_resolve_vs_cancel(self, tmp_path, ctx, action):
        """接受/拒绝 与 取消 并发：最终状态必须与某个串行顺序一致。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=24)
        r = ledger.send_invitation(c.id, "inf1", "k1", T0)
        inv_id = r.invitation_id
        ledger.close()

        entries = [
            (_resolve_worker, (str(path), inv_id, action)),
            (_cancel_worker, (str(path), c.id)),
        ]
        results = _run(ctx, entries)

        resolve_result = next(r for r in results if r[0] == action)
        cancel_result = next(r for r in results if r[0] == "cancel")
        assert cancel_result[1] == "ok", results
        # 取消先提交时，接受/拒绝必须明确失败；否则先生效再被作废。
        assert resolve_result[1] == "ok" or (
            resolve_result[1] == "exc"
            and resolve_result[2] == "CampaignCancelledError"
        ), results

        ledger = InviteLedger(path)
        try:
            final = ledger.get_invitation(inv_id, T0)
            state = ledger.get_campaign_state(c.id, T0)
            assert state.status is CampaignStatus.CANCELLED
            if action == "accept":
                # 已接受的邀请会被取消作废；取消先提交则邀请被作废。
                assert final.status is InvitationStatus.VOID
            else:
                # 拒绝先提交则保持已拒绝（规则不作废已拒绝）；
                # 取消先提交则待回复被作废。
                assert final.status in (
                    InvitationStatus.REJECTED,
                    InvitationStatus.VOID,
                )
            assert state.occupying_count == 0
        finally:
            ledger.close()
        _assert_consistent(path, c.id, T0)


class TestRestartDurability:
    def test_reopen_between_operations(self, tmp_path):
        """进程重启（重新打开数据文件）后状态与未过期幂等键照常有效。"""
        ledger, path = _fresh_ledger(tmp_path)
        c = ledger.create_campaign("brandA", T0, deadline_hours=48)
        r = ledger.send_invitation(c.id, "inf1", "k1", T0)
        ledger.close()

        now = T0 + timedelta(hours=1)
        ledger = InviteLedger(path)
        replay = ledger.send_invitation(c.id, "inf1", "k1", now)
        assert replay.replayed and replay.invitation_id == r.invitation_id
        ledger.accept(r.invitation_id, now)
        ledger.close()

        ledger = InviteLedger(path)
        state = ledger.get_campaign_state(c.id, now)
        assert state.status is CampaignStatus.ACCEPTED
        assert state.occupying_count == 1
        ledger.close()
        _assert_consistent(path, c.id, now)
