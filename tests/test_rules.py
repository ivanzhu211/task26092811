"""Single-process business rule tests."""

import pytest

from invite_ledger import (
    CampaignStatus,
    ErrorCode,
    InvitationLedger,
    InvitationStatus,
)

HOUR = 3600.0


@pytest.fixture()
def ledger(tmp_path):
    led = InvitationLedger(str(tmp_path / "ledger.db"))
    yield led
    led.close()


@pytest.fixture()
def campaign_id(ledger):
    return ledger.create_campaign("brandA", now=0.0).value


def test_ttl_validation(ledger):
    assert ledger.create_campaign("b", now=0.0, accept_ttl_hours=0).error == ErrorCode.INVALID_TTL
    assert ledger.create_campaign("b", now=0.0, accept_ttl_hours=169).error == ErrorCode.INVALID_TTL
    assert ledger.create_campaign("b", now=0.0, accept_ttl_hours=1).ok
    assert ledger.create_campaign("b", now=0.0, accept_ttl_hours=168).ok
    assert ledger.create_campaign("b", now=0.0, accept_ttl_hours=72.5).error == ErrorCode.INVALID_TTL


def test_default_ttl_is_72(ledger):
    cid = ledger.create_campaign("b", now=0.0).value
    assert ledger.get_campaign(cid, now=0.0).value.accept_ttl_hours == 72


def test_accept_just_before_deadline(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=1000.0).value
    deadline = 1000.0 + 72 * HOUR
    assert ledger.accept_invitation("t1", inv, now=deadline - 1).ok
    inv_obj = ledger.list_invitations(campaign_id, now=deadline - 1)[0]
    assert inv_obj.status == InvitationStatus.ACCEPTED
    assert inv_obj.deadline == deadline


def test_accept_exactly_at_deadline_fails(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=1000.0).value
    deadline = 1000.0 + 72 * HOUR
    res = ledger.accept_invitation("t1", inv, now=deadline)
    assert not res.ok and res.error == ErrorCode.INVITATION_NOT_PENDING
    inv_obj = ledger.list_invitations(campaign_id, now=deadline)[0]
    assert inv_obj.status == InvitationStatus.EXPIRED
    assert inv_obj.expired_at == deadline


def test_decline_exactly_at_deadline_fails(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    res = ledger.decline_invitation("t1", inv, now=72 * HOUR)
    assert not res.ok and res.error == ErrorCode.INVITATION_NOT_PENDING


def test_expired_untouched_invitation_frees_slot_and_status(ledger, campaign_id):
    # One invitation sent earlier expires first; 24 others stay pending.
    expiring = ledger.send_invitation("brandA", campaign_id, "tX", "kX", now=0.0).value
    for i in range(24):
        assert ledger.send_invitation("brandA", campaign_id, f"t{i}", f"k{i}", now=1.0).ok
    assert ledger.get_campaign(campaign_id, now=1.0).value.occupied_slots == 25

    later = 72 * HOUR  # exactly at deadline: expired
    view = ledger.get_campaign(campaign_id, now=later).value
    assert view.occupied_slots == 24
    # Still INVITED because 24 pending remain; expire the rest too.
    assert view.status == CampaignStatus.INVITED
    inv_obj = [i for i in ledger.list_invitations(campaign_id, now=later) if i.id == expiring][0]
    assert inv_obj.status == InvitationStatus.EXPIRED
    assert inv_obj.expired_at == 72 * HOUR


def test_expired_invitation_leaves_campaign_open(ledger, campaign_id):
    ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0)
    view = ledger.get_campaign(campaign_id, now=72 * HOUR).value
    assert view.status == CampaignStatus.OPEN
    assert view.occupied_slots == 0


def test_expired_talent_can_be_reinvited(ledger, campaign_id):
    ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0)
    later = 72 * HOUR
    res = ledger.send_invitation("brandA", campaign_id, "t1", "k2", now=later)
    assert res.ok


def test_declined_talent_cannot_be_reinvited(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    assert ledger.decline_invitation("t1", inv, now=10.0).ok
    res = ledger.send_invitation("brandA", campaign_id, "t1", "k2", now=20.0)
    assert not res.ok and res.error == ErrorCode.TALENT_DECLINED


def test_withdrawn_talent_can_be_reinvited(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    assert ledger.withdraw_invitation("brandA", inv, now=10.0).ok
    assert ledger.send_invitation("brandA", campaign_id, "t1", "k2", now=20.0).ok


def test_one_occupied_invitation_per_talent(ledger, campaign_id):
    ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0)
    res = ledger.send_invitation("brandA", campaign_id, "t1", "k2", now=1.0)
    assert not res.ok and res.error == ErrorCode.TALENT_OCCUPIED


def test_capacity_limit_25(ledger, campaign_id):
    for i in range(25):
        assert ledger.send_invitation("brandA", campaign_id, f"t{i}", f"k{i}", now=0.0).ok
    res = ledger.send_invitation("brandA", campaign_id, "t26", "k26", now=0.0)
    assert not res.ok and res.error == ErrorCode.CAPACITY_FULL
    assert ledger.get_campaign(campaign_id, now=0.0).value.occupied_slots == 25


def test_campaign_status_transitions(ledger, campaign_id):
    assert ledger.get_campaign(campaign_id, now=0.0).value.status == CampaignStatus.OPEN
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    assert ledger.get_campaign(campaign_id, now=0.0).value.status == CampaignStatus.INVITED
    ledger.accept_invitation("t1", inv, now=1.0)
    assert ledger.get_campaign(campaign_id, now=1.0).value.status == CampaignStatus.ACCEPTED


def test_cancel_voids_pending_and_accepted(ledger, campaign_id):
    inv1 = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    inv2 = ledger.send_invitation("brandA", campaign_id, "t2", "k2", now=0.0).value
    ledger.accept_invitation("t1", inv1, now=1.0)
    assert ledger.cancel_campaign("brandA", campaign_id, now=2.0).ok
    view = ledger.get_campaign(campaign_id, now=2.0).value
    assert view.status == CampaignStatus.CANCELLED
    assert view.occupied_slots == 0
    statuses = {i.id: i.status for i in ledger.list_invitations(campaign_id, now=2.0)}
    assert statuses[inv1] == InvitationStatus.VOIDED
    assert statuses[inv2] == InvitationStatus.VOIDED
    # All operations on a cancelled campaign fail.
    assert ledger.send_invitation("brandA", campaign_id, "t3", "k3", now=3.0).error == ErrorCode.CAMPAIGN_CANCELLED
    assert ledger.cancel_campaign("brandA", campaign_id, now=3.0).error == ErrorCode.CAMPAIGN_CANCELLED
    assert ledger.accept_invitation("t2", inv2, now=3.0).error == ErrorCode.INVITATION_NOT_PENDING
    assert ledger.withdraw_invitation("brandA", inv2, now=3.0).error == ErrorCode.INVITATION_NOT_PENDING


def test_idempotency_replay_success_and_rejection(ledger, campaign_id):
    r1 = ledger.send_invitation("brandA", campaign_id, "t1", "key1", now=0.0)
    r2 = ledger.send_invitation("brandA", campaign_id, "t1", "key1", now=HOUR)
    assert r1.ok and r2.ok and r1.value == r2.value
    assert len(ledger.list_invitations(campaign_id, now=HOUR)) == 1

    # First attempt rejected by rules; replay returns the same rejection.
    inv = ledger.send_invitation("brandA", campaign_id, "t2", "key2", now=0.0).value
    ledger.decline_invitation("t2", inv, now=1.0)
    r3 = ledger.send_invitation("brandA", campaign_id, "t2", "key3", now=2.0)
    assert not r3.ok and r3.error == ErrorCode.TALENT_DECLINED
    r4 = ledger.send_invitation("brandA", campaign_id, "t2", "key3", now=3.0)
    assert not r4.ok and r4.error == ErrorCode.TALENT_DECLINED


def test_idempotency_conflict(ledger, campaign_id):
    ledger.send_invitation("brandA", campaign_id, "t1", "key1", now=0.0)
    res = ledger.send_invitation("brandA", campaign_id, "t2", "key1", now=1.0)
    assert not res.ok and res.error == ErrorCode.IDEMPOTENCY_CONFLICT
    cid2 = ledger.create_campaign("brandA", now=0.0).value
    res = ledger.send_invitation("brandA", cid2, "t1", "key1", now=1.0)
    assert not res.ok and res.error == ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_key_scoped_per_brand(ledger, campaign_id):
    cid2 = ledger.create_campaign("brandB", now=0.0).value
    r1 = ledger.send_invitation("brandA", campaign_id, "t1", "shared", now=0.0)
    r2 = ledger.send_invitation("brandB", cid2, "t1", "shared", now=0.0)
    assert r1.ok and r2.ok and r1.value != r2.value


def test_idempotency_key_expires_after_24h(ledger, campaign_id):
    r1 = ledger.send_invitation("brandA", campaign_id, "t1", "key1", now=0.0)
    inv1 = r1.value
    ledger.withdraw_invitation("brandA", inv1, now=10.0)
    later = 24 * HOUR + 1
    r2 = ledger.send_invitation("brandA", campaign_id, "t1", "key1", now=later)
    assert r2.ok and r2.value != inv1


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "ledger.db")
    led = InvitationLedger(path)
    cid = led.create_campaign("brandA", now=0.0).value
    inv = led.send_invitation("brandA", campaign_id := cid, talent_id="t1",
                              idempotency_key="k1", now=0.0).value
    led.close()

    led2 = InvitationLedger(path)
    # Idempotency key still active after reopen.
    r = led2.send_invitation("brandA", cid, "t1", "k1", now=1.0)
    assert r.ok and r.value == inv
    view = led2.get_campaign(cid, now=1.0).value
    assert view.status == CampaignStatus.INVITED and view.occupied_slots == 1
    led2.close()


def test_foreign_brand_cannot_operate(ledger, campaign_id):
    inv = ledger.send_invitation("brandA", campaign_id, "t1", "k1", now=0.0).value
    assert ledger.withdraw_invitation("brandB", inv, now=1.0).error == ErrorCode.FORBIDDEN
    assert ledger.cancel_campaign("brandB", campaign_id, now=1.0).error == ErrorCode.FORBIDDEN
    assert ledger.send_invitation("brandB", campaign_id, "t2", "k2", now=1.0).error == ErrorCode.FORBIDDEN
    assert ledger.accept_invitation("t2", inv, now=1.0).error == ErrorCode.FORBIDDEN
