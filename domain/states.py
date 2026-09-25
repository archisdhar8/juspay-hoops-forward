from __future__ import annotations

from enum import StrEnum


class PaymentStatus(StrEnum):
    CREATED = "created"
    REQUIRES_PAYMENT_METHOD = "requires_payment_method"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


class EventSource(StrEnum):
    APPLICATION = "application"
    BROWSER = "browser"
    WEBHOOK = "webhook"
    RECONCILIATION = "reconciliation"


class LedgerEntryType(StrEnum):
    DONATION_SUCCEEDED = "donation_succeeded"
    REFUND_COMPLETED = "refund_completed"
    PROCESSING_FEE = "processing_fee"
    DISPUTE_OPENED = "dispute_opened"
    DISPUTE_WON = "dispute_won"
    DISPUTE_LOST = "dispute_lost"


ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(
        {
            PaymentStatus.REQUIRES_PAYMENT_METHOD,
            PaymentStatus.PROCESSING,
            PaymentStatus.SUCCEEDED,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.REQUIRES_PAYMENT_METHOD: frozenset(
        {
            PaymentStatus.PROCESSING,
            PaymentStatus.SUCCEEDED,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.PROCESSING: frozenset(
        {
            PaymentStatus.SUCCEEDED,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.SUCCEEDED: frozenset(
        {
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
            PaymentStatus.DISPUTED,
        }
    ),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset(
        {
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
            PaymentStatus.DISPUTED,
        }
    ),
    PaymentStatus.DISPUTED: frozenset(
        {PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED}
    ),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}


class InvalidStateTransition(ValueError):
    pass


class UnknownPaymentStatus(ValueError):
    pass


def ensure_payment_transition(
    previous: str | PaymentStatus, next_status: str | PaymentStatus
) -> None:
    previous_status = PaymentStatus(previous)
    resolved_next = PaymentStatus(next_status)
    if previous_status == resolved_next:
        return
    if resolved_next not in ALLOWED_PAYMENT_TRANSITIONS[previous_status]:
        raise InvalidStateTransition(
            f"Payment cannot transition from {previous_status.value} to {resolved_next.value}."
        )


def map_provider_status(value: str | None) -> PaymentStatus:
    normalized = (value or "").strip().lower()
    aliases = {
        "requires_confirmation": PaymentStatus.REQUIRES_PAYMENT_METHOD,
        "requires_customer_action": PaymentStatus.REQUIRES_PAYMENT_METHOD,
        "requires_capture": PaymentStatus.PROCESSING,
        "partially_captured": PaymentStatus.PROCESSING,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return PaymentStatus(normalized)
    except ValueError as exc:
        raise UnknownPaymentStatus(f"Unknown provider payment status: {normalized}") from exc
