import asyncio

from domain.states import PaymentStatus
from services.hyperswitch import HyperswitchProvider, _extract_checkout_url
from services.payment_provider import PaymentCreationRequest


def test_extracts_nested_payment_link() -> None:
    data = {"payment_link": {"link": "https://sandbox.example/checkout"}}
    assert _extract_checkout_url(data) == "https://sandbox.example/checkout"


def test_extracts_flat_payment_link() -> None:
    data = {"payment_link": "https://sandbox.example/checkout"}
    assert _extract_checkout_url(data) == "https://sandbox.example/checkout"


def test_hyperswitch_adapter_maps_internal_request_and_response(monkeypatch) -> None:
    monkeypatch.setenv("HYPERSWITCH_API_KEY", "snd_test_only")
    provider = HyperswitchProvider()
    captured = {}

    async def fake_request(method: str, path: str, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {
            "payment_id": "pay_adapter",
            "status": "requires_payment_method",
            "amount": 2500,
            "currency": "USD",
            "payment_link": {"link": "https://sandbox.example/checkout"},
            "metadata": {
                "donation_id": "don_test",
                "payment_attempt_id": "att_test",
                "campaign_id": "equipment",
            },
        }

    monkeypatch.setattr(provider, "_request", fake_request)
    payment = asyncio.run(
        provider.create_payment(
            PaymentCreationRequest(
                donation_id="don_test",
                attempt_id="att_test",
                amount_cents=2500,
                contribution_amount_cents=2500,
                fee_coverage_cents=0,
                currency="USD",
                campaign_id="equipment",
                campaign_name="Equipment that lasts",
                campaign_short_name="Equipment",
                donor_name="Test Donor",
                donor_email="donor@example.com",
                anonymous_publicly=False,
                return_url="https://example.test/payment/return",
            )
        )
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/payments"
    assert captured["json"]["metadata"]["donation_id"] == "don_test"
    assert payment.payment_id == "pay_adapter"
    assert payment.status == PaymentStatus.REQUIRES_PAYMENT_METHOD
    assert payment.amount_cents == 2500
