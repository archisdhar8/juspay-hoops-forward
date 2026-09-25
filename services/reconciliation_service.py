from __future__ import annotations

from time import perf_counter

from sqlalchemy.orm import Session

from core.logging import log_payment_event
from domain.states import EventSource, PaymentStatus
from services.payment_provider import PaymentProvider
from services.payment_service import (
    VerificationResult,
    apply_provider_payment,
    get_bound_attempt,
)


async def reconcile_payment_attempt(
    *,
    session: Session,
    donation_id: str,
    attempt_id: str,
    provider: PaymentProvider,
    source: EventSource = EventSource.RECONCILIATION,
) -> VerificationResult:
    donation, attempt = get_bound_attempt(session, donation_id, attempt_id)
    if not attempt.hyperswitch_payment_id:
        raise ValueError("The payment attempt has no Hyperswitch payment ID.")
    started = perf_counter()
    previous_status = attempt.status
    provider_payment = await provider.retrieve_payment(attempt.hyperswitch_payment_id)
    apply_provider_payment(
        session=session,
        donation=donation,
        attempt=attempt,
        provider_payment=provider_payment,
        source=source,
        event_type="payment_reconciled",
        reason="authoritative_provider_retrieval",
    )
    session.commit()
    destination = f"/donations/{donation.id}/confirmation?attempt_id={attempt.id}"
    log_payment_event(
        "payment_reconciled",
        donation_id=donation.id,
        payment_attempt_id=attempt.id,
        hyperswitch_payment_id=attempt.hyperswitch_payment_id,
        state_transition=f"{previous_status}->{provider_payment.status.value}",
        latency_ms=round((perf_counter() - started) * 1000),
        request_result="success",
        source=source.value,
    )
    return VerificationResult(
        donation_id=donation.id,
        attempt_id=attempt.id,
        status=PaymentStatus(attempt.status),
        destination=destination,
    )
