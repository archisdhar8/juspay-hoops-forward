from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from domain.states import PaymentStatus


@dataclass(frozen=True)
class PaymentCreationRequest:
    donation_id: str
    attempt_id: str
    amount_cents: int
    contribution_amount_cents: int
    fee_coverage_cents: int
    currency: str
    campaign_id: str
    campaign_name: str
    campaign_short_name: str
    donor_name: str
    donor_email: str
    anonymous_publicly: bool
    return_url: str


@dataclass(frozen=True)
class ProviderPayment:
    payment_id: str
    status: PaymentStatus
    amount_cents: int
    currency: str
    checkout_url: str | None = None
    payment_method: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    safe_metadata: dict[str, Any] = field(default_factory=dict)


class PaymentProviderError(RuntimeError):
    """Safe provider-independent payment failure."""


class AmbiguousPaymentProviderError(PaymentProviderError):
    """The provider may have accepted the request but no response was received."""


class PaymentProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def create_payment(
        self, request: PaymentCreationRequest
    ) -> ProviderPayment: ...

    async def retrieve_payment(self, payment_id: str) -> ProviderPayment: ...

    async def refund_payment(
        self, payment_id: str, amount_cents: int | None = None
    ) -> ProviderPayment: ...

    def parse_webhook(self, payload: bytes) -> dict[str, Any]: ...

    def verify_webhook(self, payload: bytes, signature: str) -> bool: ...
