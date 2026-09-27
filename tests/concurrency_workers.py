"""Worker entry points run in separate OS processes against the same SQLite file."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invite_ledger import InvitationLedger  # noqa: E402


def _serialize(result):
    return {
        "ok": result.ok,
        "value": result.value,
        "error": None if result.error is None else result.error.value,
    }


def worker_send(db_path, barrier, out_queue, brand_id, campaign_id, talent_id, key, now):
    ledger = InvitationLedger(db_path)
    try:
        barrier.wait(timeout=30)
        out_queue.put(_serialize(ledger.send_invitation(brand_id, campaign_id, talent_id, key, now)))
    finally:
        ledger.close()


def worker_accept(db_path, barrier, out_queue, talent_id, invitation_id, now):
    ledger = InvitationLedger(db_path)
    try:
        barrier.wait(timeout=30)
        out_queue.put(_serialize(ledger.accept_invitation(talent_id, invitation_id, now)))
    finally:
        ledger.close()


def worker_decline(db_path, barrier, out_queue, talent_id, invitation_id, now):
    ledger = InvitationLedger(db_path)
    try:
        barrier.wait(timeout=30)
        out_queue.put(_serialize(ledger.decline_invitation(talent_id, invitation_id, now)))
    finally:
        ledger.close()


def worker_withdraw(db_path, barrier, out_queue, brand_id, invitation_id, now):
    ledger = InvitationLedger(db_path)
    try:
        barrier.wait(timeout=30)
        out_queue.put(_serialize(ledger.withdraw_invitation(brand_id, invitation_id, now)))
    finally:
        ledger.close()


def worker_cancel(db_path, barrier, out_queue, brand_id, campaign_id, now):
    ledger = InvitationLedger(db_path)
    try:
        barrier.wait(timeout=30)
        out_queue.put(_serialize(ledger.cancel_campaign(brand_id, campaign_id, now)))
    finally:
        ledger.close()
