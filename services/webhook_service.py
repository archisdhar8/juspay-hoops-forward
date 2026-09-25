from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import (
    Donation,
    LedgerEntry,
    PaymentAttempt,
    PaymentEvent,
    WebhookEvent,
    utc_now,
)
from domain.states import EventSource, LedgerEntryType, PaymentStatus
from services.hyperswitch import normalize_payment_response
from services.ledger_service import record_dispute_effect, record_refund_completed
from services.payment_provider import PaymentProvider, ProviderPayment
from services.payment_service import (
    PaymentOperationError,
    apply_provider_payment,
    record_payment_state_transition,
)


PAYMENT_EVENTS = {
    "payment_succeeded",
    "payment_failed",
    "payment_processing",
    "payment_cancelled",
    "payment_authorized",
    "payment_captured",
    "action_required",
}


@dataclass(frozen=True)
class WebhookResult:
    event_id: str
    status: str
    duplicate: bool = False


class WebhookProcessingError(ValueError):
    pass


async def store_and_process_webhook(
    *,
    session: Session,
    provider: PaymentProvider,
    payload: dict[str, Any],
    raw_payload: bytes,
) -> WebhookResult:
    envelope = _parse_envelope(payload)
    existing = session.get(WebhookEvent, envelope["event_id"])
    if existing is not None:
        existing.duplicate_count += 1
        existing.safe_metadata = {
            **(existing.safe_metadata or {}),
            "duplicate_received": True,
        }
        session.commit()
        if existing.processing_status != "stored":
            return WebhookResult(existing.event_id, existing.processing_status, duplicate=True)
        stored = existing
    else:
        stored = WebhookEvent(
            event_id=envelope["event_id"],
            event_type=envelope["event_type"],
            hyperswitch_payment_id=envelope["payment_id"],
            payload_hash=hashlib.sha256(raw_payload).hexdigest(),
            processing_status="stored",
            safe_metadata={
                "resource_updated": envelope["resource_updated"],
                "resource_type": envelope["resource_type"],
            },
        )
        session.add(stored)
        session.commit()

    attempt = session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.hyperswitch_payment_id == envelope["payment_id"]
        )
    )
    if attempt is None:
        stored.processing_status = "needs_attention"
        stored.safe_metadata = {
            **stored.safe_metadata,
            "issue": "unknown_payment_reference",
        }
        stored.processed_at = utc_now()
        session.commit()
        return WebhookResult(stored.event_id, stored.processing_status)

    donation = session.get(Donation, attempt.donation_id)
    if donation is None:
        stored.processing_status = "needs_attention"
        stored.safe_metadata = {**stored.safe_metadata, "issue": "missing_donation"}
        stored.processed_at = utc_now()
        session.commit()
        return WebhookResult(stored.event_id, stored.processing_status)

    session.add(
        PaymentEvent(
            donation_id=donation.id,
            payment_attempt_id=attempt.id,
            external_hyperswitch_id=attempt.hyperswitch_payment_id,
            event_type="webhook_received",
            source=EventSource.WEBHOOK.value,
            previous_state=attempt.status,
            next_state=attempt.status,
            reason=envelope["event_type"],
            safe_metadata={"webhook_event_id": stored.event_id},
        )
    )

    if _is_out_of_order(session, stored):
        stored.processing_status = "ignored_out_of_order"
        stored.safe_metadata = {**stored.safe_metadata, "issue": "out_of_order"}
        stored.processed_at = utc_now()
        session.add(
            PaymentEvent(
                donation_id=donation.id,
                payment_attempt_id=attempt.id,
                external_hyperswitch_id=attempt.hyperswitch_payment_id,
                event_type="webhook_out_of_order",
                source=EventSource.WEBHOOK.value,
                previous_state=attempt.status,
                next_state=attempt.status,
                reason="older_resource_update_timestamp",
                safe_metadata={"webhook_event_id": stored.event_id},
            )
        )
        session.commit()
        return WebhookResult(stored.event_id, stored.processing_status)

    try:
        if envelope["event_type"] in PAYMENT_EVENTS:
            provider_payment = await _resolve_payment(
                provider, envelope["resource"], attempt
            )
            apply_provider_payment(
                session=session,
                donation=donation,
                attempt=attempt,
                provider_payment=provider_payment,
                source=EventSource.WEBHOOK,
                event_type="payment_verified",
                reason=f"signed_webhook:{stored.event_id}",
            )
        elif envelope["event_type"] == "refund_succeeded":
            _apply_refund(session, stored, donation, attempt, envelope["resource"])
        elif envelope["event_type"].startswith("dispute_"):
            _apply_dispute(session, stored, donation, attempt, envelope)
        else:
            session.add(
                PaymentEvent(
                    donation_id=donation.id,
                    payment_attempt_id=attempt.id,
                    external_hyperswitch_id=attempt.hyperswitch_payment_id,
                    event_type=envelope["event_type"],
                    source=EventSource.WEBHOOK.value,
                    previous_state=attempt.status,
                    next_state=attempt.status,
                    reason="stored_without_financial_effect",
                    safe_metadata={"webhook_event_id": stored.event_id},
                )
            )
    except (PaymentOperationError, ValueError) as exc:
        stored.processing_status = "needs_attention"
        stored.safe_metadata = {
            **stored.safe_metadata,
            "issue": "processing_rejected",
            "error_category": type(exc).__name__,
        }
        stored.processed_at = utc_now()
        session.commit()
        return WebhookResult(stored.event_id, stored.processing_status)

    stored.processing_status = "processed"
    stored.processed_at = utc_now()
    session.commit()
    return WebhookResult(stored.event_id, stored.processing_status)


async def _resolve_payment(
    provider: PaymentProvider,
    resource: dict[str, Any],
    attempt: PaymentAttempt,
) -> ProviderPayment:
    ambiguous = any(
        [
            not resource.get("status"),
            resource.get("amount") is None,
            not resource.get("currency"),
            not isinstance(resource.get("metadata"), dict),
        ]
    )
    if ambiguous:
        if not attempt.hyperswitch_payment_id:
            raise WebhookProcessingError("The webhook cannot be reconciled.")
        return await provider.retrieve_payment(attempt.hyperswitch_payment_id)
    return normalize_payment_response(resource)


def _apply_refund(
    session: Session,
    webhook: WebhookEvent,
    donation: Donation,
    attempt: PaymentAttempt,
    resource: dict[str, Any],
) -> None:
    refund_id = str(resource.get("refund_id") or resource.get("id") or webhook.event_id)
    amount = _integer_amount(resource.get("refund_amount") or resource.get("amount"))
    if amount <= 0:
        raise WebhookProcessingError("The refund amount is missing.")
    record_refund_completed(
        session=session,
        donation=donation,
        attempt=attempt,
        event_id=webhook.event_id,
        refund_id=refund_id,
        amount_cents=amount,
    )
    total_refunded = abs(
        sum(
            entry.amount_cents
            for entry in session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.payment_attempt_id == attempt.id,
                    LedgerEntry.entry_type == LedgerEntryType.REFUND_COMPLETED.value,
                )
            )
        )
    )
    next_status = (
        PaymentStatus.REFUNDED
        if total_refunded >= attempt.amount_cents
        else PaymentStatus.PARTIALLY_REFUNDED
    )
    record_payment_state_transition(
        session=session,
        donation=donation,
        attempt=attempt,
        next_status=next_status,
        event_type="refund_completed",
        source=EventSource.WEBHOOK,
        external_id=refund_id,
        reason=f"signed_webhook:{webhook.event_id}",
    )


def _apply_dispute(
    session: Session,
    webhook: WebhookEvent,
    donation: Donation,
    attempt: PaymentAttempt,
    envelope: dict[str, Any],
) -> None:
    resource = envelope["resource"]
    dispute_id = str(resource.get("dispute_id") or resource.get("id") or webhook.event_id)
    amount = _integer_amount(resource.get("amount") or resource.get("dispute_amount"))
    if amount <= 0:
        raise WebhookProcessingError("The dispute amount is missing.")
    event_type = envelope["event_type"]
    if event_type == "dispute_opened":
        ledger_type = LedgerEntryType.DISPUTE_OPENED
        next_status = PaymentStatus.DISPUTED
    elif event_type == "dispute_won":
        ledger_type = LedgerEntryType.DISPUTE_WON
        next_status = PaymentStatus.SUCCEEDED
    elif event_type == "dispute_lost":
        ledger_type = LedgerEntryType.DISPUTE_LOST
        next_status = PaymentStatus.REFUNDED
    else:
        return
    record_dispute_effect(
        session=session,
        donation=donation,
        attempt=attempt,
        event_id=webhook.event_id,
        dispute_id=dispute_id,
        entry_type=ledger_type,
        amount_cents=amount,
    )
    record_payment_state_transition(
        session=session,
        donation=donation,
        attempt=attempt,
        next_status=next_status,
        event_type=event_type,
        source=EventSource.WEBHOOK,
        external_id=dispute_id,
        reason=f"signed_webhook:{webhook.event_id}",
    )


def _parse_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    event_id = str(payload.get("event_id") or "").strip()
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    resource = content.get("object") if isinstance(content.get("object"), dict) else content
    if not resource:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        resource = data.get("object") if isinstance(data.get("object"), dict) else data
    payment_id = str(
        resource.get("payment_id")
        or resource.get("payment_intent_id")
        or (resource.get("id") if event_type.startswith("payment_") else "")
        or ""
    )
    if not event_id or len(event_id) > 120:
        raise WebhookProcessingError("The webhook event ID is missing or invalid.")
    if not event_type or len(event_type) > 80:
        raise WebhookProcessingError("The webhook event type is missing or invalid.")
    if not payment_id or len(payment_id) > 100:
        raise WebhookProcessingError("The webhook payment reference is missing or invalid.")
    updated = resource.get("updated") or resource.get("modified_at") or resource.get("updated_at")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payment_id": payment_id,
        "resource": resource,
        "resource_type": str(content.get("type") or "unknown"),
        "resource_updated": str(updated or ""),
    }


def _is_out_of_order(session: Session, current: WebhookEvent) -> bool:
    current_updated = _parse_timestamp((current.safe_metadata or {}).get("resource_updated"))
    if current_updated is None:
        return False
    prior_events = session.scalars(
        select(WebhookEvent).where(
            WebhookEvent.hyperswitch_payment_id == current.hyperswitch_payment_id,
            WebhookEvent.event_id != current.event_id,
            WebhookEvent.processing_status == "processed",
        )
    )
    for prior in prior_events:
        prior_updated = _parse_timestamp((prior.safe_metadata or {}).get("resource_updated"))
        if prior_updated is not None and prior_updated >= current_updated:
            return True
    return False


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _integer_amount(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("order_amount")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
