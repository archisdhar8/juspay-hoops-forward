from core.database import session_scope
from domain.models import Donation, PaymentAttempt
from domain.states import LedgerEntryType, PaymentStatus
from models.donation import parse_donation
from services.donation_service import create_donation_intent
from services.ledger_service import (
    calculate_ledger_summary,
    record_dispute_effect,
    record_donation_succeeded,
    record_refund_completed,
)


def _succeeded_records() -> tuple[str, str]:
    value = parse_donation(
        campaign_id="scholarships",
        donor_name="Ledger Donor",
        donor_email="ledger@example.com",
        amount="25",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, value)
        session.flush()
        attempt = PaymentAttempt(
            donation_id=donation.id,
            hyperswitch_payment_id="pay_ledger",
            idempotency_key=donation.checkout_idempotency_key,
            amount_cents=2500,
            currency="USD",
            status=PaymentStatus.SUCCEEDED.value,
        )
        donation.status = PaymentStatus.SUCCEEDED.value
        session.add(attempt)
        session.flush()
        record_donation_succeeded(session, donation, attempt)
        return donation.id, attempt.id


def test_ledger_derives_gross_refunds_disputes_and_net() -> None:
    donation_id, attempt_id = _succeeded_records()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and attempt
        record_refund_completed(
            session=session,
            donation=donation,
            attempt=attempt,
            event_id="evt_refund_ledger",
            refund_id="ref_ledger",
            amount_cents=500,
        )
        record_dispute_effect(
            session=session,
            donation=donation,
            attempt=attempt,
            event_id="evt_dispute_open",
            dispute_id="dp_ledger",
            entry_type=LedgerEntryType.DISPUTE_OPENED,
            amount_cents=1000,
        )
        summary = calculate_ledger_summary(session)
        assert summary.gross_contributions_cents == 2500
        assert summary.refunds_cents == 500
        assert summary.disputes_cents == 1000
        assert summary.fees_cents == 0
        assert summary.net_available_cents == 1000

        record_dispute_effect(
            session=session,
            donation=donation,
            attempt=attempt,
            event_id="evt_dispute_won",
            dispute_id="dp_ledger",
            entry_type=LedgerEntryType.DISPUTE_WON,
            amount_cents=1000,
        )
        restored = calculate_ledger_summary(session)
        assert restored.disputes_cents == 0
        assert restored.net_available_cents == 2000


def test_success_ledger_effect_is_written_once() -> None:
    donation_id, attempt_id = _succeeded_records()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and attempt
        first = record_donation_succeeded(session, donation, attempt)
        second = record_donation_succeeded(session, donation, attempt)
        assert first.id == second.id
