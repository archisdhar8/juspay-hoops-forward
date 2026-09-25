import asyncio

import pytest
from sqlalchemy import func, select

from core.database import session_scope
from domain.models import Donation, PaymentAttempt, PaymentEvent
from domain.states import PaymentStatus
from models.donation import parse_donation
from services.donation_service import create_donation_intent
from services.hyperswitch import HyperswitchError, HyperswitchProvider
from services.payment_provider import PaymentCreationRequest, ProviderPayment
from services.payment_service import (
    PaymentOperationError,
    create_or_reuse_payment_attempt,
    prepare_payment_retry,
    verify_payment_attempt,
)


class FakeProvider:
    configured = True

    def __init__(self) -> None:
        self.create_calls = 0
        self.retrieve_result: ProviderPayment | None = None

    async def create_payment(self, request: PaymentCreationRequest) -> ProviderPayment:
        self.create_calls += 1
        return ProviderPayment(
            payment_id=f"pay_idempotent_{self.create_calls}",
            status=PaymentStatus.REQUIRES_PAYMENT_METHOD,
            amount_cents=request.amount_cents,
            currency=request.currency,
            checkout_url="https://sandbox.example/checkout",
        )

    async def retrieve_payment(self, _payment_id: str) -> ProviderPayment:
        assert self.retrieve_result is not None
        return self.retrieve_result


def _persist_donation() -> str:
    donation_input = parse_donation(
        campaign_id="equipment",
        donor_name="Test Donor",
        donor_email="donor@example.com",
        amount="25",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, donation_input)
        session.commit()
        return donation.id


def test_donation_and_payment_attempt_are_separate_records() -> None:
    donation_id = _persist_donation()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation is not None
        assert donation.payment_attempts == []
        assert donation.intended_amount_cents == 2500


def test_duplicate_payment_submission_reuses_one_attempt() -> None:
    donation_id = _persist_donation()
    provider = FakeProvider()

    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation
        first = asyncio.run(
            create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base="https://example.test",
                provider=provider,
            )
        )

    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation
        second = asyncio.run(
            create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base="https://example.test",
                provider=provider,
            )
        )
        attempt_count = session.scalar(select(func.count(PaymentAttempt.id)))

    assert first.id == second.id
    assert first.hyperswitch_payment_id == second.hyperswitch_payment_id
    assert provider.create_calls == 1
    assert attempt_count == 1


def test_verification_rejects_provider_amount_mismatch() -> None:
    donation_id = _persist_donation()
    provider = FakeProvider()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation
        attempt = asyncio.run(
            create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base="https://example.test",
                provider=provider,
            )
        )
        attempt_id = attempt.id

    provider.retrieve_result = ProviderPayment(
        payment_id="pay_idempotent_1",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=9999,
        currency="USD",
        safe_metadata={
            "donation_id": donation_id,
            "payment_attempt_id": attempt_id,
        },
    )
    with session_scope() as session:
        with pytest.raises(PaymentOperationError, match="amount does not match"):
            asyncio.run(
                verify_payment_attempt(
                    session=session,
                    donation_id=donation_id,
                    attempt_id=attempt_id,
                    provider=provider,
                )
            )
    with session_scope() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        assert attempt and attempt.status == PaymentStatus.REQUIRES_PAYMENT_METHOD.value
        rejected_events = session.scalars(
            select(PaymentEvent).where(
                PaymentEvent.event_type == "payment_verification_rejected"
            )
        ).all()
        assert len(rejected_events) == 1


def test_failed_retry_creates_new_attempt_not_new_donation() -> None:
    donation_id = _persist_donation()
    provider = FakeProvider()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation
        first = asyncio.run(
            create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base="https://example.test",
                provider=provider,
            )
        )
        first_id = first.id

    provider.retrieve_result = ProviderPayment(
        payment_id="pay_idempotent_1",
        status=PaymentStatus.FAILED,
        amount_cents=2500,
        currency="USD",
        safe_metadata={
            "donation_id": donation_id,
            "payment_attempt_id": first_id,
        },
    )
    with session_scope() as session:
        asyncio.run(
            verify_payment_attempt(
                session=session,
                donation_id=donation_id,
                attempt_id=first_id,
                provider=provider,
            )
        )

    with session_scope() as session:
        donation = prepare_payment_retry(session, donation_id, first_id)
        session.commit()
        second = asyncio.run(
            create_or_reuse_payment_attempt(
                session=session,
                donation=donation,
                idempotency_key=donation.checkout_idempotency_key,
                return_url_base="https://example.test",
                provider=provider,
            )
        )
        second_id = second.id

    provider.retrieve_result = ProviderPayment(
        payment_id="pay_idempotent_2",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=2500,
        currency="USD",
        safe_metadata={
            "donation_id": donation_id,
            "payment_attempt_id": second_id,
        },
    )
    with session_scope() as session:
        asyncio.run(
            verify_payment_attempt(
                session=session,
                donation_id=donation_id,
                attempt_id=second_id,
                provider=provider,
            )
        )

    with session_scope() as session:
        assert session.scalar(select(func.count(Donation.id))) == 1
        assert session.scalar(select(func.count(PaymentAttempt.id))) == 2
        donation = session.get(Donation, donation_id)
        assert donation and donation.status == PaymentStatus.SUCCEEDED.value
        with pytest.raises(PaymentOperationError, match="cannot be paid again"):
            prepare_payment_retry(session, donation_id, first_id)


def test_missing_api_key_fails_before_network(monkeypatch) -> None:
    monkeypatch.delenv("HYPERSWITCH_API_KEY", raising=False)
    provider = HyperswitchProvider()

    with pytest.raises(HyperswitchError, match="API key"):
        asyncio.run(provider.retrieve_payment("pay_test"))
