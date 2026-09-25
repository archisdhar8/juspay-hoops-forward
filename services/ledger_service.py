from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Donation, LedgerEntry, PaymentAttempt
from domain.states import LedgerEntryType


@dataclass(frozen=True)
class LedgerSummary:
    gross_contributions_cents: int
    pending_funds_cents: int
    refunds_cents: int
    disputes_cents: int
    dispute_losses_cents: int
    fees_cents: int
    net_available_cents: int


def record_donation_succeeded(
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
) -> LedgerEntry:
    return _append_once(
        session=session,
        effect_key=f"donation_succeeded:{attempt.id}",
        donation_id=donation.id,
        attempt_id=attempt.id,
        entry_type=LedgerEntryType.DONATION_SUCCEEDED,
        amount_cents=attempt.amount_cents,
        currency=attempt.currency,
        external_reference=attempt.hyperswitch_payment_id,
    )


def record_refund_completed(
    *,
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    event_id: str,
    refund_id: str,
    amount_cents: int,
) -> LedgerEntry:
    return _append_once(
        session=session,
        effect_key=f"refund_completed:{event_id}",
        donation_id=donation.id,
        attempt_id=attempt.id,
        entry_type=LedgerEntryType.REFUND_COMPLETED,
        amount_cents=-abs(amount_cents),
        currency=attempt.currency,
        external_reference=refund_id,
    )


def record_dispute_effect(
    *,
    session: Session,
    donation: Donation,
    attempt: PaymentAttempt,
    event_id: str,
    dispute_id: str,
    entry_type: LedgerEntryType,
    amount_cents: int,
) -> LedgerEntry:
    sign = 1 if entry_type == LedgerEntryType.DISPUTE_WON else -1
    return _append_once(
        session=session,
        effect_key=f"{entry_type.value}:{event_id}",
        donation_id=donation.id,
        attempt_id=attempt.id,
        entry_type=entry_type,
        amount_cents=sign * abs(amount_cents),
        currency=attempt.currency,
        external_reference=dispute_id,
    )


def calculate_ledger_summary(
    session: Session, campaign_id: str | None = None
) -> LedgerSummary:
    statement = select(LedgerEntry).join(Donation)
    if campaign_id:
        statement = statement.where(Donation.campaign_id == campaign_id)
    entries = list(session.scalars(statement.order_by(LedgerEntry.created_at)))

    gross = sum(
        entry.amount_cents
        for entry in entries
        if entry.entry_type == LedgerEntryType.DONATION_SUCCEEDED.value
    )
    refunds = abs(
        sum(
            entry.amount_cents
            for entry in entries
            if entry.entry_type == LedgerEntryType.REFUND_COMPLETED.value
        )
    )
    fees = abs(
        sum(
            entry.amount_cents
            for entry in entries
            if entry.entry_type == LedgerEntryType.PROCESSING_FEE.value
        )
    )

    dispute_entries: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        if entry.entry_type in {
            LedgerEntryType.DISPUTE_OPENED.value,
            LedgerEntryType.DISPUTE_WON.value,
            LedgerEntryType.DISPUTE_LOST.value,
        }:
            dispute_entries[entry.external_event_id or entry.id].append(entry)

    pending_disputes = 0
    dispute_losses = 0
    for lifecycle in dispute_entries.values():
        latest = lifecycle[-1]
        opened_amount = next(
            (
                abs(entry.amount_cents)
                for entry in lifecycle
                if entry.entry_type == LedgerEntryType.DISPUTE_OPENED.value
            ),
            abs(latest.amount_cents),
        )
        if latest.entry_type == LedgerEntryType.DISPUTE_OPENED.value:
            pending_disputes += opened_amount
        elif latest.entry_type == LedgerEntryType.DISPUTE_LOST.value:
            dispute_losses += abs(latest.amount_cents) or opened_amount

    succeeded_attempt_ids = {
        entry.payment_attempt_id
        for entry in entries
        if entry.entry_type == LedgerEntryType.DONATION_SUCCEEDED.value
    }
    pending = sum(
        attempt.amount_cents
        for attempt in session.scalars(select(PaymentAttempt))
        if attempt.id not in succeeded_attempt_ids
        and attempt.status in {"created", "requires_payment_method", "processing"}
    )
    net = gross - refunds - fees - pending_disputes - dispute_losses
    return LedgerSummary(
        gross_contributions_cents=gross,
        pending_funds_cents=pending,
        refunds_cents=refunds,
        disputes_cents=pending_disputes,
        dispute_losses_cents=dispute_losses,
        fees_cents=fees,
        net_available_cents=net,
    )


def _append_once(
    *,
    session: Session,
    effect_key: str,
    donation_id: str,
    attempt_id: str | None,
    entry_type: LedgerEntryType,
    amount_cents: int,
    currency: str,
    external_reference: str | None,
) -> LedgerEntry:
    existing = session.scalar(
        select(LedgerEntry).where(LedgerEntry.effect_key == effect_key)
    )
    if existing is not None:
        return existing
    entry = LedgerEntry(
        donation_id=donation_id,
        payment_attempt_id=attempt_id,
        entry_type=entry_type.value,
        amount_cents=amount_cents,
        currency=currency,
        external_event_id=external_reference,
        effect_key=effect_key,
    )
    session.add(entry)
    session.flush()
    return entry
