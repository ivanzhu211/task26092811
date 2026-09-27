"""单进程业务规则测试。"""

from datetime import datetime, timedelta

import pytest

from invite_ledger import (
    CampaignCancelledError,
    CampaignStatus,
    InvalidDeadlineHoursError,
    InvitationNotPendingError,
    InvitationStatus,
    InviteLedger,
)

T0 = datetime(2026, 9, 28, 12, 0, 0)


@pytest.fixture()
def ledger(tmp_path):
    led = InviteLedger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture()
def campaign(ledger):
    return ledger.create_campaign("brandA", T0, deadline_hours=24)


def _send(ledger, campaign_id, influencer, key, now=T0):
    result = ledger.send_invitation(campaign_id, influencer, key, now)
    assert result.ok, result.reason
    return result.invitation_id


class TestDeadlineHours:
    def test_default_is_72(self, ledger):
        c = ledger.create_campaign("b", T0)
        assert c.deadline_hours == 72

    @pytest.mark.parametrize("hours", [1, 72, 168])
    def test_valid_range(self, ledger, hours):
        c = ledger.create_campaign("b", T0, deadline_hours=hours)
        assert c.deadline_hours == hours

    @pytest.mark.parametrize("hours", [0, 169, -1, 1.5, "24", True])
    def test_invalid(self, ledger, hours):
        with pytest.raises(InvalidDeadlineHoursError):
            ledger.create_campaign("b", T0, deadline_hours=hours)


class TestExpiry:
    def test_accept_just_before_deadline(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        before = T0 + timedelta(hours=24) - timedelta(microseconds=1)
        updated = ledger.accept(inv, before)
        assert updated.status is InvitationStatus.ACCEPTED

    def test_accept_exactly_at_deadline_fails(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        at_deadline = T0 + timedelta(hours=24)
        with pytest.raises(InvitationNotPendingError):
            ledger.accept(inv, at_deadline)
        assert ledger.get_invitation(inv, at_deadline).status is (
            InvitationStatus.EXPIRED
        )

    def test_reject_exactly_at_deadline_fails(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        with pytest.raises(InvitationNotPendingError):
            ledger.reject(inv, T0 + timedelta(hours=24))

    def test_expired_at_equals_deadline(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        later = T0 + timedelta(hours=100)
        expired = ledger.get_invitation(inv, later)
        assert expired.status is InvitationStatus.EXPIRED
        assert expired.resolved_at == T0 + timedelta(hours=24)

    def test_untouched_expired_invitation(self, ledger, campaign):
        """过期但没被碰过的邀请：不占名额、不挡重新邀请、活动不停在已邀请。"""
        inv = _send(ledger, campaign.id, "inf1", "k1")
        later = T0 + timedelta(hours=25)
        state = ledger.get_campaign_state(campaign.id, later)
        assert state.status is CampaignStatus.OPEN
        assert state.occupying_count == 0
        result = ledger.send_invitation(campaign.id, "inf1", "k2", later)
        assert result.ok
        assert result.invitation_id != inv


class TestQuota:
    def test_quota_is_25(self, ledger, campaign):
        for i in range(25):
            _send(ledger, campaign.id, f"inf{i}", f"k{i}")
        result = ledger.send_invitation(campaign.id, "inf26", "k26", T0)
        assert not result.ok
        assert result.reason == "quota_exceeded"
        state = ledger.get_campaign_state(campaign.id, T0)
        assert state.occupying_count == 25

    def test_rejected_and_withdrawn_free_slots(self, ledger, campaign):
        ids = [_send(ledger, campaign.id, f"inf{i}", f"k{i}") for i in range(25)]
        ledger.reject(ids[0], T0)
        ledger.withdraw(ids[1], T0)
        assert ledger.get_campaign_state(campaign.id, T0).occupying_count == 23
        assert ledger.send_invitation(campaign.id, "infA", "kA", T0).ok
        assert ledger.send_invitation(campaign.id, "infB", "kB", T0).ok
        assert not ledger.send_invitation(campaign.id, "infC", "kC", T0).ok

    def test_expired_frees_slot(self, ledger, campaign):
        for i in range(25):
            _send(ledger, campaign.id, f"inf{i}", f"k{i}")
        later = T0 + timedelta(hours=24)
        result = ledger.send_invitation(campaign.id, "infNew", "kNew", later)
        assert result.ok


class TestPerInfluencerRules:
    def test_duplicate_active_invitation_rejected(self, ledger, campaign):
        _send(ledger, campaign.id, "inf1", "k1")
        result = ledger.send_invitation(campaign.id, "inf1", "k2", T0)
        assert not result.ok
        assert result.reason == "duplicate_active_invitation"

    def test_rejected_influencer_cannot_be_reinvited(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        ledger.reject(inv, T0)
        result = ledger.send_invitation(campaign.id, "inf1", "k2", T0)
        assert not result.ok
        assert result.reason == "influencer_rejected_before"

    def test_withdrawn_influencer_can_be_reinvited(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        ledger.withdraw(inv, T0)
        assert ledger.send_invitation(campaign.id, "inf1", "k2", T0).ok

    def test_expired_influencer_can_be_reinvited(self, ledger, campaign):
        _send(ledger, campaign.id, "inf1", "k1")
        later = T0 + timedelta(hours=24)
        assert ledger.send_invitation(campaign.id, "inf1", "k2", later).ok

    def test_accepted_influencer_blocks_new_invitation(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        ledger.accept(inv, T0)
        result = ledger.send_invitation(campaign.id, "inf1", "k2", T0)
        assert not result.ok
        assert result.reason == "duplicate_active_invitation"


class TestCampaignStatus:
    def test_open_invited_accepted_flow(self, ledger, campaign):
        assert ledger.get_campaign_state(campaign.id, T0).status is (
            CampaignStatus.OPEN
        )
        inv = _send(ledger, campaign.id, "inf1", "k1")
        assert ledger.get_campaign_state(campaign.id, T0).status is (
            CampaignStatus.INVITED
        )
        ledger.accept(inv, T0)
        assert ledger.get_campaign_state(campaign.id, T0).status is (
            CampaignStatus.ACCEPTED
        )

    def test_status_moves_with_time(self, ledger, campaign):
        _send(ledger, campaign.id, "inf1", "k1")
        later = T0 + timedelta(hours=24)
        assert ledger.get_campaign_state(campaign.id, later).status is (
            CampaignStatus.OPEN
        )


class TestCancel:
    def test_cancel_voids_pending_and_accepted(self, ledger, campaign):
        inv1 = _send(ledger, campaign.id, "inf1", "k1")
        inv2 = _send(ledger, campaign.id, "inf2", "k2")
        ledger.accept(inv1, T0)
        now = T0 + timedelta(hours=1)
        ledger.cancel_campaign(campaign.id, now)
        assert ledger.get_invitation(inv1, now).status is InvitationStatus.VOID
        assert ledger.get_invitation(inv2, now).status is InvitationStatus.VOID
        state = ledger.get_campaign_state(campaign.id, now)
        assert state.status is CampaignStatus.CANCELLED
        assert state.occupying_count == 0

    def test_cancel_keeps_expired_expired(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        later = T0 + timedelta(hours=24)
        ledger.cancel_campaign(campaign.id, later)
        got = ledger.get_invitation(inv, later)
        assert got.status is InvitationStatus.EXPIRED
        assert got.resolved_at == T0 + timedelta(hours=24)

    def test_operations_fail_after_cancel(self, ledger, campaign):
        inv = _send(ledger, campaign.id, "inf1", "k1")
        now = T0 + timedelta(hours=1)
        ledger.cancel_campaign(campaign.id, now)
        with pytest.raises(CampaignCancelledError):
            ledger.accept(inv, now)
        with pytest.raises(CampaignCancelledError):
            ledger.reject(inv, now)
        with pytest.raises(CampaignCancelledError):
            ledger.withdraw(inv, now)
        with pytest.raises(CampaignCancelledError):
            ledger.cancel_campaign(campaign.id, now)
        result = ledger.send_invitation(campaign.id, "inf2", "k2", now)
        assert not result.ok
        assert result.reason == "campaign_cancelled"


class TestIdempotency:
    def test_replay_returns_same_result(self, ledger, campaign):
        r1 = ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        r2 = ledger.send_invitation(
            campaign.id, "inf1", "k1", T0 + timedelta(hours=1)
        )
        assert r1.ok and r2.ok
        assert r2.replayed
        assert r2.invitation_id == r1.invitation_id
        assert len(ledger.list_invitations(campaign.id, T0)) == 1
        assert ledger.get_campaign_state(campaign.id, T0).occupying_count == 1

    def test_replay_of_rejection(self, ledger, campaign):
        for i in range(25):
            _send(ledger, campaign.id, f"inf{i}", f"k{i}")
        r1 = ledger.send_invitation(campaign.id, "infX", "kX", T0)
        assert not r1.ok and r1.reason == "quota_exceeded"
        # 即使后来腾出名额，重放仍返回第一次的拒绝。
        ledger.withdraw(1, T0)
        r2 = ledger.send_invitation(
            campaign.id, "infX", "kX", T0 + timedelta(hours=1)
        )
        assert not r2.ok
        assert r2.reason == "quota_exceeded"
        assert r2.replayed

    def test_conflict_on_different_influencer(self, ledger, campaign):
        ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        result = ledger.send_invitation(campaign.id, "inf2", "k1", T0)
        assert not result.ok
        assert result.reason == "idempotency_conflict"

    def test_conflict_on_different_campaign(self, ledger, campaign):
        other = ledger.create_campaign("brandA", T0)
        ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        result = ledger.send_invitation(other.id, "inf1", "k1", T0)
        assert not result.ok
        assert result.reason == "idempotency_conflict"

    def test_keys_are_scoped_per_brand(self, ledger, campaign):
        other_brand = ledger.create_campaign("brandB", T0)
        ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        result = ledger.send_invitation(other_brand.id, "inf1", "k1", T0)
        assert result.ok

    def test_key_expires_after_24h(self, ledger, campaign):
        r1 = ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        later = T0 + timedelta(hours=24)
        # 旧邀请此时已过期，键也过期：当新键处理，产生新邀请。
        r2 = ledger.send_invitation(campaign.id, "inf1", "k1", later)
        assert r2.ok
        assert not r2.replayed
        assert r2.invitation_id != r1.invitation_id

    def test_key_alive_at_23h59m(self, ledger, campaign):
        r1 = ledger.send_invitation(campaign.id, "inf1", "k1", T0)
        later = T0 + timedelta(hours=24) - timedelta(microseconds=1)
        r2 = ledger.send_invitation(campaign.id, "inf1", "k1", later)
        assert r2.replayed
        assert r2.invitation_id == r1.invitation_id


class TestPersistence:
    def test_state_survives_reopen(self, tmp_path):
        path = tmp_path / "ledger.db"
        led = InviteLedger(path)
        c = led.create_campaign("brandA", T0, deadline_hours=48)
        inv = _send(led, c.id, "inf1", "k1")
        led.close()

        led2 = InviteLedger(path)
        now = T0 + timedelta(hours=1)
        state = led2.get_campaign_state(c.id, now)
        assert state.status is CampaignStatus.INVITED
        replay = led2.send_invitation(c.id, "inf1", "k1", now)
        assert replay.replayed
        assert replay.invitation_id == inv
        led2.accept(inv, now)
        led2.close()

        led3 = InviteLedger(path)
        assert led3.get_invitation(inv, now).status is InvitationStatus.ACCEPTED
        led3.close()
