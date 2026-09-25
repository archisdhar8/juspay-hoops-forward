from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Donation, LedgerEntry, PaymentAttempt, PaymentEvent, WebhookEvent
from domain.states import LedgerEntryType, PaymentStatus
from services.ledger_service import LedgerSummary, calculate_ledger_summary


@dataclass(frozen=True)
class OperationsDonationRow:
    donation_id: str
    campaign: str
    donor_display: str
    gross_display: str
    donation_status: str
    attempt_status: str
    hyperswitch_payment_id: str
    created_at: datetime
    updated_at: datetime
    confirmation_source: str
    refund_dispute_status: str
    reconciliation_status: str
    latest_attempt_id: str | None


@dataclass(frozen=True)
class AttentionItem:
    code: str
    label: str
    detail: str
    donation_id: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True)
class OperationsOverview:
    donations: list[OperationsDonationRow]
    attention: list[AttentionItem]
    ledger: LedgerSummary


def build_operations_overview(session: Session) -> OperationsOverview:
    donations = list(session.scalars(select(Donation).order_by(Donation.created_at.desc())))
    rows = [_donation_row(session, donation) for donation in donations]
    return OperationsOverview(
        donations=rows,
        attention=find_attention_items(session),
        ledger=calculate_ledger_summary(session),
    )


def donation_operations_detail(
    session: Session, donation_id: str
) -> tuple[Donation, list[PaymentAttempt], list[PaymentEvent], list[LedgerEntry]]:
    donation = session.get(Donation, donation_id)
    if donation is None:
        raise LookupError("Donation not found.")
    attempts = list(
        session.scalars(
            select(PaymentAttempt)
            .where(PaymentAttempt.donation_id == donation.id)
            .order_by(PaymentAttempt.created_at)
        )
    )
    events = list(
        session.scalars(
            select(PaymentEvent)
            .where(PaymentEvent.donation_id == donation.id)
            .order_by(PaymentEvent.created_at)
        )
    )
    ledger = list(
        session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.donation_id == donation.id)
            .order_by(LedgerEntry.created_at)
        )
    )
    return donation, attempts, events, ledger


def find_attention_items(session: Session) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    donations = {item.id: item for item in session.scalars(select(Donation))}
    attempts = list(session.scalars(select(PaymentAttempt)))
    now = datetime.now(timezone.utc)
    for attempt in attempts:
        donation = donations.get(attempt.donation_id)
        if donation is None:
            continue
        if (
            attempt.status == PaymentStatus.SUCCEEDED.value
            and donation.status != PaymentStatus.SUCCEEDED.value
        ):
            items.append(
                AttentionItem(
                    "remote_success_local_pending",
                    "Payment succeeded but donation is pending",
                    "A succeeded attempt is not reflected on the donation aggregate.",
                    donation.id,
                    attempt.id,
                )
            )
        updated_at = _aware(attempt.updated_at)
        if (
            attempt.status == PaymentStatus.PROCESSING.value
            and updated_at < now - timedelta(minutes=10)
        ):
            items.append(
                AttentionItem(
                    "processing_stuck",
                    "Payment stuck in processing",
                    "The attempt has remained processing for more than ten minutes.",
                    donation.id,
                    attempt.id,
                )
            )

    for webhook in session.scalars(select(WebhookEvent)):
        issue = (webhook.safe_metadata or {}).get("issue")
        if webhook.duplicate_count:
            items.append(
                AttentionItem(
                    "duplicate_webhook",
                    "Duplicate webhook",
                    f"{webhook.event_id} was delivered {webhook.duplicate_count + 1} times.",
                )
            )
        if webhook.processing_status == "needs_attention":
            items.append(
                AttentionItem(
                    str(issue or "webhook_processing_issue"),
                    "Webhook requires investigation",
                    f"{webhook.event_id}: {issue or 'processing rejected'}.",
                )
            )

    invalid_transitions = session.scalars(
        select(PaymentEvent).where(PaymentEvent.event_type == "invalid_state_transition")
    )
    for event in invalid_transitions:
        items.append(
            AttentionItem(
                "invalid_state_transition",
                "Invalid payment transition",
                event.reason or "An event attempted to regress payment state.",
                event.donation_id,
                event.payment_attempt_id,
            )
        )

    failed_receipts = session.scalars(
        select(PaymentEvent).where(PaymentEvent.event_type == "receipt_generation_failed")
    )
    for event in failed_receipts:
        items.append(
            AttentionItem(
                "receipt_generation_failed",
                "Receipt generation failed",
                event.reason or "The donor confirmation artifact was not generated.",
                event.donation_id,
                event.payment_attempt_id,
            )
        )

    for donation in donations.values():
        if donation.status not in {
            PaymentStatus.PARTIALLY_REFUNDED.value,
            PaymentStatus.REFUNDED.value,
        }:
            continue
        refund_total = abs(
            sum(
                entry.amount_cents
                for entry in session.scalars(
                    select(LedgerEntry).where(
                        LedgerEntry.donation_id == donation.id,
                        LedgerEntry.entry_type == LedgerEntryType.REFUND_COMPLETED.value,
                    )
                )
            )
        )
        if refund_total == 0 or refund_total > donation.intended_amount_cents:
            items.append(
                AttentionItem(
                    "refund_mismatch",
                    "Refund mismatch",
                    "Donation refund state does not match its ledger effects.",
                    donation.id,
                )
            )
    return items


def donor_display(donation: Donation) -> str:
    if donation.anonymous_publicly:
        return "Anonymous donor"
    words = [word for word in donation.donor_contact.name.split() if word]
    if not words:
        return "Named donor"
    return " ".join(f"{word[0]}{'•' * min(max(len(word) - 1, 2), 6)}" for word in words)


def _donation_row(session: Session, donation: Donation) -> OperationsDonationRow:
    attempts = list(
        session.scalars(
            select(PaymentAttempt)
            .where(PaymentAttempt.donation_id == donation.id)
            .order_by(PaymentAttempt.created_at.desc())
        )
    )
    latest = attempts[0] if attempts else None
    gross_cents = sum(
        entry.amount_cents
        for entry in session.scalars(
            select(LedgerEntry).where(
                LedgerEntry.donation_id == donation.id,
                LedgerEntry.entry_type == LedgerEntryType.DONATION_SUCCEEDED.value,
            )
        )
    )
    status = latest.status if latest else "not_started"
    reconciliation = (
        f"confirmed by {latest.confirmation_source}"
        if latest and latest.confirmation_source
        else "unconfirmed"
    )
    return OperationsDonationRow(
        donation_id=donation.id,
        campaign=donation.campaign.name,
        donor_display=donor_display(donation),
        gross_display=_usd(gross_cents),
        donation_status=donation.status,
        attempt_status=status,
        hyperswitch_payment_id=(latest.hyperswitch_payment_id if latest else None) or "—",
        created_at=donation.created_at,
        updated_at=donation.updated_at,
        confirmation_source=(latest.confirmation_source if latest else None) or "—",
        refund_dispute_status=(
            status
            if status
            in {
                PaymentStatus.PARTIALLY_REFUNDED.value,
                PaymentStatus.REFUNDED.value,
                PaymentStatus.DISPUTED.value,
            }
            else "none"
        ),
        reconciliation_status=reconciliation,
        latest_attempt_id=latest.id if latest else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
