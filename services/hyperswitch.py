from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import httpx

from models.donation import Donation


class HyperswitchError(RuntimeError):
    """Safe application-level error for Hyperswitch failures."""


@dataclass(frozen=True)
class CreatedPayment:
    payment_id: str
    checkout_url: str


class HyperswitchClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("HYPERSWITCH_API_KEY", "").strip()
        self.base_url = os.getenv(
            "HYPERSWITCH_BASE_URL", "https://sandbox.hyperswitch.io"
        ).rstrip("/")
        self.profile_id = os.getenv("HYPERSWITCH_PROFILE_ID", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def create_payment(
        self,
        *,
        donation: Donation,
        donation_reference: str,
        return_url: str,
    ) -> CreatedPayment:
        self._require_configuration()
        payload: dict[str, Any] = {
            "amount": donation.total_cents,
            "currency": "USD",
            "payment_link": True,
            "confirm": False,
            "capture_method": "automatic",
            "return_url": return_url,
            "description": f"Hoops Forward - {donation.campaign['short_name']}",
            "customer": {
                "name": "Anonymous donor" if donation.anonymous else donation.donor_name,
                "email": donation.donor_email,
            },
            "metadata": {
                "donation_reference": donation_reference,
                "campaign_id": donation.campaign_id,
                "anonymous": str(donation.anonymous).lower(),
                "gift_cents": donation.gift_cents,
                "fee_cents": donation.fee_cents,
            },
            "payment_link_config": {
                "theme": "#f97316",
                "seller_name": "Hoops Forward",
                "show_card_form_by_default": True,
                "payment_button_text": "Complete donation",
                "transaction_details": [
                    {"key": "Campaign", "value": donation.campaign["short_name"]},
                    {"key": "Donation reference", "value": donation_reference},
                ],
            },
        }
        if self.profile_id:
            payload["profile_id"] = self.profile_id

        data = await self._request("POST", "/payments", json=payload)
        payment_id = str(data.get("payment_id", ""))
        checkout_url = _extract_checkout_url(data)
        if not payment_id or not checkout_url:
            raise HyperswitchError("Hyperswitch did not return a usable checkout session.")
        return CreatedPayment(payment_id=payment_id, checkout_url=checkout_url)

    async def retrieve_payment(self, payment_id: str) -> dict[str, Any]:
        self._require_configuration()
        if not payment_id.startswith("pay_") or len(payment_id) > 100:
            raise HyperswitchError("The payment reference is invalid.")
        return await self._request("GET", f"/payments/{payment_id}")

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
            raise HyperswitchError(
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
            return response.json()
        except ValueError as exc:
            raise HyperswitchError("Hyperswitch returned an unreadable response.") from exc

    def _require_configuration(self) -> None:
        if not self.configured:
            raise HyperswitchError(
                "The sandbox API key has not been configured on this deployment."
            )


def _extract_checkout_url(data: dict[str, Any]) -> str:
    payment_link = data.get("payment_link")
    if isinstance(payment_link, dict):
        return str(payment_link.get("link") or payment_link.get("secure_link") or "")
    if isinstance(payment_link, str) and payment_link.startswith("http"):
        return payment_link
    return str(data.get("payment_link_url") or "")
