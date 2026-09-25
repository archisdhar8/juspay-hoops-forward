import asyncio

from sqlalchemy import select

from core.database import session_scope
from domain.models import LedgerEntry, PaymentAttempt, PaymentEvent
from domain.states import LedgerEntryType, PaymentStatus
from models.donation import parse_donation
from services.donation_service import create_donation_intent
from services.payment_provider import ProviderPayment
from services.reconciliation_service import reconcile_payment_attempt


class ReconciliationProvider:
    configured = True

    def __init__(self, payment: ProviderPayment) -> None:
        self.payment = payment

    async def retrieve_payment(self, payment_id: str) -> ProviderPayment:
        assert payment_id == self.payment.payment_id
        return self.payment


def test_reconciliation_applies_authoritative_result_once() -> None:
    donation_input = parse_donation(
        campaign_id="equipment",
        donor_name="Reconcile Donor",
        donor_email="reconcile@example.com",
        amount="25",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, donation_input)
        session.flush()
        attempt = PaymentAttempt(
            donation_id=donation.id,
            hyperswitch_payment_id="pay_reconcile",
            idempotency_key=donation.checkout_idempotency_key,
            amount_cents=2500,
            currency="USD",
            status=PaymentStatus.PROCESSING.value,
        )
        donation.status = PaymentStatus.PROCESSING.value
        session.add(attempt)
        session.flush()
        donation_id, attempt_id = donation.id, attempt.id

    provider = ReconciliationProvider(
        ProviderPayment(
            payment_id="pay_reconcile",
            status=PaymentStatus.SUCCEEDED,
            amount_cents=2500,
            currency="USD",
            safe_metadata={
                "donation_id": donation_id,
                "payment_attempt_id": attempt_id,
            },
        )
    )
    with session_scope() as session:
        result = asyncio.run(
            reconcile_payment_attempt(
                session=session,
                donation_id=donation_id,
                attempt_id=attempt_id,
                provider=provider,
            )
        )

    with session_scope() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        ledger = list(
            session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.entry_type == LedgerEntryType.DONATION_SUCCEEDED.value
                )
            )
        )
        reconciled = list(
            session.scalars(
                select(PaymentEvent).where(
                    PaymentEvent.event_type == "payment_reconciled"
                )
            )
        )

    assert result.status == PaymentStatus.SUCCEEDED
    assert attempt and attempt.confirmation_source == "reconciliation"
    assert len(ledger) == 1
    assert len(reconciled) == 1
