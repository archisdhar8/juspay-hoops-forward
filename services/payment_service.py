from __future__ import annotations

from dataclasses import dataclass
import secrets
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.logging import log_payment_event
from domain.models import Donation, PaymentAttempt, PaymentEvent, utc_now
from domain.states import (
    EventSource,
    InvalidStateTransition,
    PaymentStatus,
    ensure_payment_transition,
)
from services.payment_provider import (
    AmbiguousPaymentProviderError,
    PaymentCreationRequest,
    PaymentProvider,
    PaymentProviderError,
    ProviderPayment,
)
from services.ledger_service import record_donation_succeeded


class PaymentOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    donation_id: str
    attempt_id: str
    status: PaymentStatus
    destination: str


async def create_or_reuse_payment_attempt(
    *,
    session: Session,
    donation: Donation,
    idempotency_key: str,
    return_url_base: str,
    provider: PaymentProvider,
) -> PaymentAttempt:
    if idempotency_key != donation.checkout_idempotency_key:
        raise PaymentOperationError("The checkout request is invalid or expired.")

    existing = session.scalar(
        select(PaymentAttempt).where(PaymentAttempt.idempotency_key == idempotency_key)
    )
    if existing is not None:
        log_payment_event(
            "payment_creation_reused",
            donation_id=donation.id,
            payment_attempt_id=existing.id,
            hyperswitch_payment_id=existing.hyperswitch_payment_id,
            state=existing.status,
        )
        if existing.checkout_url:
            return existing
        raise PaymentOperationError(
            "This payment is already being prepared. Please wait before retrying."
        )

    attempt = PaymentAttempt(
        donation_id=donation.id,
        idempotency_key=idempotency_key,
        amount_cents=donation.intended_amount_cents,
        currency=donation.currency,
        status=PaymentStatus.CREATED.value,
    )
    session.add(attempt)
    session.flush()
    session.add(
        PaymentEvent(
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            event_type="payment_creation_started",
            source=EventSource.APPLICATION.value,
            previous_state=None,
            next_state=PaymentStatus.CREATED.value,
            safe_metadata={"amount_cents": attempt.amount_cents, "currency": attempt.currency},
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        existing = session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.idempotency_key == idempotency_key)
        )
        if existing is not None and existing.checkout_url:
            return existing
        raise PaymentOperationError("This payment is already being prepared.") from exc

    return_url = (
        f"{return_url_base}/payment/return?donation_id={donation.id}&attempt_id={attempt.id}"
    )
    request = PaymentCreationRequest(
        donation_id=donation.id,
        attempt_id=attempt.id,
        amount_cents=donation.intended_amount_cents,
        contribution_amount_cents=donation.contribution_amount_cents,
        fee_coverage_cents=donation.fee_coverage_cents,
        currency=donation.currency,
        campaign_id=donation.campaign_id,
        campaign_name=donation.campaign.name,
        campaign_short_name=donation.designation,
        donor_name=donation.donor_contact.name,
        donor_email=donation.donor_contact.email,
        anonymous_publicly=donation.anonymous_publicly,
        return_url=return_url,
    )
    started = perf_counter()
    try:
        provider_payment = await provider.create_payment(request)
    except AmbiguousPaymentProviderError:
        _transition_attempt(
            session=session,
            donation=donation,
            attempt=attempt,
            next_status=PaymentStatus.PROCESSING,
            event_type="payment_creation_outcome_unknown",
            source=EventSource.APPLICATION,
            reason="provider_response_not_received",
        )
        session.commit()
        log_payment_event(
            "payment_creation_outcome_unknown",
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            latency_ms=round((perf_counter() - started) * 1000),
            error_category="ambiguous_provider_result",
        )
        raise
    except PaymentProviderError:
        _transition_attempt(
            session=session,
            donation=donation,
            attempt=attempt,
            next_status=PaymentStatus.FAILED,
            event_type="payment_creation_failed",
            source=EventSource.APPLICATION,
            reason="provider_request_failed",
        )
        session.commit()
        log_payment_event(
            "payment_creation_failed",
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            latency_ms=round((perf_counter() - started) * 1000),
            error_category="provider_request_failed",
        )
        raise

    attempt.hyperswitch_payment_id = provider_payment.payment_id
    attempt.checkout_url = provider_payment.checkout_url
    _transition_attempt(
        session=session,
        donation=donation,
        attempt=attempt,
        next_status=provider_payment.status,
        event_type="payment_created",
        source=EventSource.APPLICATION,
        external_id=provider_payment.payment_id,
    )
    session.commit()
    log_payment_event(
        "payment_created",
        donation_id=donation.id,
        payment_attempt_id=attempt.id,
        hyperswitch_payment_id=provider_payment.payment_id,
        state_transition=f"created->{provider_payment.status.value}",
        latency_ms=round((perf_counter() - started) * 1000),
        request_result="success",
    )
    return attempt


async def verify_payment_attempt(
    *,
    session: Session,
    donation_id: str,
    attempt_id: str,
    provider: PaymentProvider,
) -> VerificationResult:
    donation, attempt = _load_bound_attempt(session, donation_id, attempt_id)
    if not attempt.hyperswitch_payment_id:
        raise PaymentOperationError("The payment has not been created yet.")

    started = perf_counter()
    provider_payment = await provider.retrieve_payment(attempt.hyperswitch_payment_id)
    previous_status = attempt.status
    try:
        apply_provider_payment(
            session=session,
            donation=donation,
            attempt=attempt,
            provider_payment=provider_payment,
            source=EventSource.RECONCILIATION,
            event_type="payment_verified",
            reason="authoritative_provider_retrieval",
        )
    except PaymentOperationError:
        session.commit()
        raise
    session.commit()
    destination = _confirmation_destination(donation.id, attempt.id, provider_payment.status)
    log_payment_event(
        "payment_verified",
        donation_id=donation.id,
        payment_attempt_id=attempt.id,
        hyperswitch_payment_id=attempt.hyperswitch_payment_id,
        state_transition=f"{previous_status}->{provider_payment.status.value}",
        latency_ms=round((perf_counter() - started) * 1000),
        request_result="success",
    )
    return VerificationResult(
        donation_id=donation.id,
        attempt_id=attempt.id,
        status=provider_payment.status,
        destination=destination,
    )


def apply_provider_payment(
    *,
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    provider_payment: ProviderPayment,
    source: EventSource,
    event_type: str,
    reason: str,
) -> None:
    if provider_payment.payment_id != attempt.hyperswitch_payment_id:
        raise PaymentOperationError("The payment provider returned a mismatched payment.")
    if provider_payment.safe_metadata.get("donation_id") != donation.id:
        _record_verification_issue(
            session, donation, attempt, "donation_binding_mismatch", provider_payment
        )
        raise PaymentOperationError("The verified payment is not bound to this donation.")
    if provider_payment.safe_metadata.get("payment_attempt_id") != attempt.id:
        _record_verification_issue(
            session, donation, attempt, "attempt_binding_mismatch", provider_payment
        )
        raise PaymentOperationError("The verified payment is not bound to this attempt.")
    if provider_payment.amount_cents != attempt.amount_cents:
        _record_verification_issue(
            session, donation, attempt, "amount_mismatch", provider_payment
        )
        raise PaymentOperationError("The verified payment amount does not match the donation.")
    if provider_payment.currency != attempt.currency:
        _record_verification_issue(
            session, donation, attempt, "currency_mismatch", provider_payment
        )
        raise PaymentOperationError("The verified payment currency does not match the donation.")

    try:
        _transition_attempt(
            session=session,
            donation=donation,
            attempt=attempt,
            next_status=provider_payment.status,
            event_type=event_type,
            source=source,
            external_id=provider_payment.payment_id,
            reason=reason,
        )
    except InvalidStateTransition as exc:
        session.add(
            PaymentEvent(
                donation_id=donation.id,
                payment_attempt_id=attempt.id,
                external_hyperswitch_id=provider_payment.payment_id,
                event_type="invalid_state_transition",
                source=source.value,
                previous_state=attempt.status,
                next_state=provider_payment.status.value,
                reason=str(exc),
                safe_metadata={},
            )
        )
        raise PaymentOperationError("The payment returned an invalid state transition.") from exc

    attempt.payment_method = provider_payment.payment_method
    attempt.failure_code = provider_payment.failure_code
    attempt.failure_message = provider_payment.failure_message
    attempt.confirmation_source = source.value
    if provider_payment.status == PaymentStatus.SUCCEEDED:
        record_donation_succeeded(session, donation, attempt)


def get_bound_attempt(
    session: Session, donation_id: str, attempt_id: str
) -> tuple[Donation, PaymentAttempt]:
    return _load_bound_attempt(session, donation_id, attempt_id)


def record_payment_state_transition(
    *,
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    next_status: PaymentStatus,
    event_type: str,
    source: EventSource,
    external_id: str | None = None,
    reason: str | None = None,
) -> None:
    _transition_attempt(
        session=session,
        donation=donation,
        attempt=attempt,
        next_status=next_status,
        event_type=event_type,
        source=source,
        external_id=external_id,
        reason=reason,
    )


def prepare_payment_retry(
    session: Session, donation_id: str, failed_attempt_id: str
) -> Donation:
    donation, attempt = _load_bound_attempt(session, donation_id, failed_attempt_id)
    if donation.status == PaymentStatus.SUCCEEDED.value:
        raise PaymentOperationError("A completed donation cannot be paid again.")
    if PaymentStatus(attempt.status) not in {
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    }:
        raise PaymentOperationError("This payment is not eligible for a new attempt.")
    previous_status = donation.status
    donation.checkout_idempotency_key = f"idem_{secrets.token_urlsafe(24)}"
    donation.status = PaymentStatus.CREATED.value
    donation.updated_at = utc_now()
    session.add(
        PaymentEvent(
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            external_hyperswitch_id=attempt.hyperswitch_payment_id,
            event_type="payment_retry_requested",
            source=EventSource.BROWSER.value,
            previous_state=previous_status,
            next_state=PaymentStatus.CREATED.value,
            reason="donor_requested_new_attempt",
            safe_metadata={},
        )
    )
    session.flush()
    return donation


def _load_bound_attempt(
    session: Session, donation_id: str, attempt_id: str
) -> tuple[Donation, PaymentAttempt]:
    donation = session.get(Donation, donation_id)
    attempt = session.get(PaymentAttempt, attempt_id)
    if donation is None or attempt is None or attempt.donation_id != donation.id:
        raise PaymentOperationError("The payment confirmation link is invalid.")
    return donation, attempt


def _transition_attempt(
    *,
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    next_status: PaymentStatus,
    event_type: str,
    source: EventSource,
    external_id: str | None = None,
    reason: str | None = None,
) -> None:
    previous_status = PaymentStatus(attempt.status)
    ensure_payment_transition(previous_status, next_status)
    if previous_status != next_status:
        attempt.status = next_status.value
        attempt.updated_at = utc_now()
    if donation.status != PaymentStatus.SUCCEEDED.value or next_status in {
        PaymentStatus.PARTIALLY_REFUNDED,
        PaymentStatus.REFUNDED,
        PaymentStatus.DISPUTED,
    }:
        donation.status = next_status.value
        donation.updated_at = utc_now()
    session.add(
        PaymentEvent(
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            external_hyperswitch_id=external_id or attempt.hyperswitch_payment_id,
            event_type=event_type,
            source=source.value,
            previous_state=previous_status.value,
            next_state=next_status.value,
            reason=reason,
            safe_metadata={},
        )
    )


def _record_verification_issue(
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    reason: str,
    provider_payment: ProviderPayment,
) -> None:
    session.add(
        PaymentEvent(
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            external_hyperswitch_id=provider_payment.payment_id,
            event_type="payment_verification_rejected",
            source=EventSource.RECONCILIATION.value,
            previous_state=attempt.status,
            next_state=attempt.status,
            reason=reason,
            safe_metadata={
                "provider_amount_cents": provider_payment.amount_cents,
                "provider_currency": provider_payment.currency,
            },
        )
    )


def _confirmation_destination(
    donation_id: str, attempt_id: str, status: PaymentStatus
) -> str:
    return f"/donations/{donation_id}/confirmation?attempt_id={attempt_id}"
