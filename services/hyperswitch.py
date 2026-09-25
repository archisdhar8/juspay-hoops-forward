from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx

from domain.states import UnknownPaymentStatus, map_provider_status
from services.payment_provider import (
    AmbiguousPaymentProviderError,
    PaymentCreationRequest,
    PaymentProviderError,
    ProviderPayment,
)


class HyperswitchError(PaymentProviderError):
    """Safe application-level error for Hyperswitch failures."""


class HyperswitchProvider:
    def __init__(self) -> None:
        self.api_key = os.getenv("HYPERSWITCH_API_KEY", "").strip()
        self.base_url = os.getenv(
            "HYPERSWITCH_BASE_URL", "https://sandbox.hyperswitch.io"
        ).rstrip("/")
        self.profile_id = os.getenv("HYPERSWITCH_PROFILE_ID", "").strip()
        self.webhook_secret = os.getenv("HYPERSWITCH_WEBHOOK_SECRET", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def create_payment(
        self, request: PaymentCreationRequest
    ) -> ProviderPayment:
        self._require_configuration()
        payload: dict[str, Any] = {
            "amount": request.amount_cents,
            "currency": request.currency,
            "payment_link": True,
            "confirm": False,
            "capture_method": "automatic",
            "return_url": request.return_url,
            "description": f"Hoops Forward - {request.campaign_short_name}",
            "customer": {
                "name": (
                    "Anonymous donor" if request.anonymous_publicly else request.donor_name
                ),
                "email": request.donor_email,
            },
            "metadata": {
                "donation_id": request.donation_id,
                "payment_attempt_id": request.attempt_id,
                "campaign_id": request.campaign_id,
                "anonymous_publicly": str(request.anonymous_publicly).lower(),
                "contribution_amount_cents": request.contribution_amount_cents,
                "fee_coverage_cents": request.fee_coverage_cents,
            },
            "payment_link_config": {
                "theme": "#f97316",
                "seller_name": "Hoops Forward",
                "show_card_form_by_default": True,
                "payment_button_text": "Complete donation",
                "transaction_details": [
                    {"key": "Campaign", "value": request.campaign_short_name},
                    {"key": "Donation ID", "value": request.donation_id},
                ],
            },
        }
        if self.profile_id:
            payload["profile_id"] = self.profile_id

        data = await self._request("POST", "/payments", json=payload)
        payment = normalize_payment_response(data)
        if not payment.payment_id or not payment.checkout_url:
            raise HyperswitchError("Hyperswitch did not return a usable checkout session.")
        return payment

    async def retrieve_payment(self, payment_id: str) -> ProviderPayment:
        self._require_configuration()
        if not payment_id.startswith("pay_") or len(payment_id) > 100:
            raise HyperswitchError("The payment reference is invalid.")
        data = await self._request("GET", f"/payments/{payment_id}")
        return normalize_payment_response(data)

    async def refund_payment(
        self, payment_id: str, amount_cents: int | None = None
    ) -> ProviderPayment:
        raise HyperswitchError("Refunds are not enabled in this prototype.")

    def parse_webhook(self, payload: bytes) -> dict[str, Any]:
        try:
            result = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HyperswitchError("The webhook payload is invalid.") from exc
        if not isinstance(result, dict):
            raise HyperswitchError("The webhook payload is invalid.")
        return result

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"), payload, hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {
            "api-key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
        except httpx.RequestError as exc:
            raise AmbiguousPaymentProviderError(
                "The payment service could not be reached. Please try again."
            ) from exc

        if response.is_error:
            message = "Hyperswitch rejected the payment request."
            try:
                body = response.json()
                error = body.get("error") or {}
                message = error.get("message") or body.get("message") or message
            except ValueError:
                pass
            raise HyperswitchError(message)

        try:
            data = response.json()
        except ValueError as exc:
            raise HyperswitchError("Hyperswitch returned an unreadable response.") from exc
        if not isinstance(data, dict):
            raise HyperswitchError("Hyperswitch returned an unreadable response.")
        return data

    def _require_configuration(self) -> None:
        if not self.configured:
            raise HyperswitchError(
                "The sandbox API key has not been configured on this deployment."
            )


HyperswitchClient = HyperswitchProvider


def normalize_payment_response(data: dict[str, Any]) -> ProviderPayment:
    amount_value = data.get("amount")
    if isinstance(amount_value, dict):
        amount_value = amount_value.get("order_amount") or amount_value.get("net_amount")
    try:
        amount_cents = int(amount_value or 0)
    except (TypeError, ValueError):
        amount_cents = 0

    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    failure_message = error.get("message") or data.get("error_message")
    try:
        status = map_provider_status(str(data.get("status") or "created"))
    except UnknownPaymentStatus as exc:
        raise HyperswitchError("Hyperswitch returned an unsupported payment status.") from exc
    return ProviderPayment(
        payment_id=str(data.get("payment_id") or data.get("id") or ""),
        status=status,
        amount_cents=amount_cents,
        currency=str(data.get("currency") or "USD").upper(),
        checkout_url=_extract_checkout_url(data) or None,
        payment_method=(str(data.get("payment_method")) if data.get("payment_method") else None),
        failure_code=(str(error.get("code")) if error.get("code") else None),
        failure_message=str(failure_message)[:300] if failure_message else None,
        safe_metadata={
            key: metadata[key]
            for key in ("donation_id", "payment_attempt_id", "campaign_id")
            if key in metadata
        },
    )


def _extract_checkout_url(data: dict[str, Any]) -> str:
    payment_link = data.get("payment_link")
    if isinstance(payment_link, dict):
        return str(payment_link.get("link") or payment_link.get("secure_link") or "")
    if isinstance(payment_link, str) and payment_link.startswith("http"):
        return payment_link
    return str(data.get("payment_link_url") or "")
