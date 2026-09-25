from __future__ import annotations

from datetime import datetime, timezone
import secrets
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domain.states import PaymentStatus


DEFAULT_ORGANIZATION_ID = "org_hoops_forward"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=utc_now, onupdate=utc_now, nullable=False
    )


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(160), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), nullable=False)
    nonprofit_status: Mapped[str] = mapped_column(String(40), nullable=False)
    receipt_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tax_disclosure: Mapped[str] = mapped_column(Text, nullable=False)


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    designation: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DonorContact(TimestampMixin, Base):
    __tablename__ = "donor_contacts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("donor")
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(254), nullable=False)


class Donation(TimestampMixin, Base):
    __tablename__ = "donations"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("don")
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    donor_contact_id: Mapped[str] = mapped_column(
        ForeignKey("donor_contacts.id"), nullable=False, index=True
    )
    intended_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    contribution_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    fee_coverage_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    designation: Mapped[str] = mapped_column(String(160), nullable=False)
    anonymous_publicly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=PaymentStatus.CREATED.value, index=True
    )
    checkout_idempotency_key: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True
    )

    donor_contact: Mapped[DonorContact] = relationship(lazy="joined")
    campaign: Mapped[Campaign] = relationship(lazy="joined")
    organization: Mapped[Organization] = relationship(lazy="joined")
    payment_attempts: Mapped[list[PaymentAttempt]] = relationship(
        back_populates="donation", order_by="PaymentAttempt.created_at"
    )

    @property
    def campaign_id_for_form(self) -> str:
        return self.campaign_id

    @property
    def gift_cents(self) -> int:
        return self.contribution_amount_cents

    @property
    def fee_cents(self) -> int:
        return self.fee_coverage_cents

    @property
    def total_cents(self) -> int:
        return self.intended_amount_cents

    @property
    def anonymous(self) -> bool:
        return self.anonymous_publicly

    @property
    def cover_fees(self) -> bool:
        return self.fee_coverage_cents > 0

    @property
    def donor_name(self) -> str:
        return self.donor_contact.name

    @property
    def donor_email(self) -> str:
        return self.donor_contact.email

    @property
    def gift_display(self) -> str:
        return _format_usd(self.contribution_amount_cents)

    @property
    def fee_display(self) -> str:
        return _format_usd(self.fee_coverage_cents)

    @property
    def total_display(self) -> str:
        return _format_usd(self.intended_amount_cents)


class PaymentAttempt(TimestampMixin, Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payment_attempt_idempotency"),
        UniqueConstraint("hyperswitch_payment_id", name="uq_hyperswitch_payment_id"),
        Index("ix_payment_attempt_donation_status", "donation_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("att")
    )
    donation_id: Mapped[str] = mapped_column(
        ForeignKey("donations.id"), nullable=False, index=True
    )
    hyperswitch_payment_id: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    payment_method: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=PaymentStatus.CREATED.value, index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(300))
    confirmation_source: Mapped[str | None] = mapped_column(String(40))

    donation: Mapped[Donation] = relationship(back_populates="payment_attempts")


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("evt")
    )
    donation_id: Mapped[str] = mapped_column(
        ForeignKey("donations.id"), nullable=False, index=True
    )
    payment_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_attempts.id"), index=True
    )
    external_hyperswitch_id: Mapped[str | None] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(40))
    next_state: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(200))
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False, index=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    hyperswitch_payment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(40), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column()


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("effect_key", name="uq_ledger_financial_effect"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("led")
    )
    donation_id: Mapped[str] = mapped_column(
        ForeignKey("donations.id"), nullable=False, index=True
    )
    payment_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_attempts.id"), index=True
    )
    entry_type: Mapped[str] = mapped_column(String(60), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    external_event_id: Mapped[str | None] = mapped_column(String(120))
    effect_key: Mapped[str] = mapped_column(String(220), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("audit")
    )
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)


def _format_usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
