import re

from fastapi.testclient import TestClient

from app import app
from core.database import session_scope
from domain.models import Donation, PaymentAttempt
from domain.states import PaymentStatus
from services.hyperswitch import HyperswitchProvider
from services.payment_provider import ProviderPayment


client = TestClient(app)


def _create_donation() -> str:
    response = client.post(
        "/review",
        data={
            "campaign_id": "scholarships",
            "amount": "50",
            "donor_name": "Architecture Review",
            "donor_email": "review@example.com",
        },
    )
    assert response.status_code == 200
    match = re.search(r'name="donation_id" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _attach_attempt(donation_id: str, status: PaymentStatus) -> str:
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        assert donation
        attempt = PaymentAttempt(
            donation_id=donation.id,
            hyperswitch_payment_id=f"pay_{status.value}",
            idempotency_key=donation.checkout_idempotency_key,
            checkout_url="https://sandbox.example/checkout",
            amount_cents=donation.intended_amount_cents,
            currency="USD",
            status=PaymentStatus.REQUIRES_PAYMENT_METHOD.value,
        )
        session.add(attempt)
        session.flush()
        return attempt.id


def test_home_page_renders() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Hoops Forward" in response.text
    assert "Sandbox demonstration" in response.text
    assert "Know what you’re funding" in response.text
    assert "$75 funds a registration scholarship" in response.text
    assert "Donate anonymously" in response.text


def test_review_persists_stable_donation_intent() -> None:
    response = client.post(
        "/review",
        data={
            "campaign_id": "scholarships",
            "amount": "50",
            "donor_name": "Archis Dhar",
            "donor_email": "archis@example.com",
            "cover_fees": "true",
        },
    )
    assert response.status_code == 200
    assert "$51.80" in response.text
    assert "Play without barriers" in response.text
    match = re.search(r'name="donation_id" value="([^"]+)"', response.text)
    assert match
    with session_scope() as session:
        donation = session.get(Donation, match.group(1))
        assert donation is not None
        assert donation.contribution_amount_cents == 5000
        assert donation.fee_coverage_cents == 180
        assert donation.intended_amount_cents == 5180


def test_review_rejects_tampered_campaign() -> None:
    response = client.post(
        "/review",
        data={
            "campaign_id": "not-real",
            "amount": "50",
            "donor_name": "Archis Dhar",
            "donor_email": "archis@example.com",
        },
    )
    assert response.status_code == 422
    assert "Choose a valid campaign" in response.text


def test_health_discloses_configuration_not_secrets() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["persistence"] == "local_sqlite"
    assert "api" not in response.text.lower()


def test_return_page_uses_only_bound_internal_ids() -> None:
    donation_id = _create_donation()
    attempt_id = _attach_attempt(donation_id, PaymentStatus.SUCCEEDED)

    response = client.get(
        f"/payment/return?donation_id={donation_id}&attempt_id={attempt_id}"
    )

    assert response.status_code == 200
    assert "Verifying contribution" in response.text
    assert donation_id in response.text
    assert "pay_succeeded" not in response.url.query.decode()


def test_server_authoritative_success_is_persisted(monkeypatch) -> None:
    donation_id = _create_donation()
    attempt_id = _attach_attempt(donation_id, PaymentStatus.SUCCEEDED)

    async def retrieve_success(_self, payment_id: str) -> ProviderPayment:
        assert payment_id == "pay_succeeded"
        return ProviderPayment(
            payment_id=payment_id,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=5000,
            currency="USD",
            payment_method="card",
            safe_metadata={
                "donation_id": donation_id,
                "payment_attempt_id": attempt_id,
            },
        )

    monkeypatch.setattr(HyperswitchProvider, "retrieve_payment", retrieve_success)
    verify = client.post(
        f"/api/donations/{donation_id}/attempts/{attempt_id}/verify"
    )
    assert verify.status_code == 200
    assert verify.json()["status"] == "succeeded"

    confirmation = client.get(verify.json()["destination"])
    assert confirmation.status_code == 200
    assert "Donation confirmed" in confirmation.text
    assert donation_id in confirmation.text
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and donation.status == "succeeded"
        assert attempt and attempt.status == "succeeded"
        assert attempt.confirmation_source == "reconciliation"


def test_server_authoritative_failure_is_persisted(monkeypatch) -> None:
    donation_id = _create_donation()
    attempt_id = _attach_attempt(donation_id, PaymentStatus.FAILED)

    async def retrieve_failure(_self, payment_id: str) -> ProviderPayment:
        assert payment_id == "pay_failed"
        return ProviderPayment(
            payment_id=payment_id,
            status=PaymentStatus.FAILED,
            amount_cents=5000,
            currency="USD",
            failure_code="card_declined",
            failure_message="The sandbox card was declined.",
            safe_metadata={
                "donation_id": donation_id,
                "payment_attempt_id": attempt_id,
            },
        )

    monkeypatch.setattr(HyperswitchProvider, "retrieve_payment", retrieve_failure)
    verify = client.post(
        f"/api/donations/{donation_id}/attempts/{attempt_id}/verify"
    )
    assert verify.status_code == 200
    assert verify.json()["status"] == "failed"
    confirmation = client.get(verify.json()["destination"])
    assert "payment didn’t go through" in confirmation.text


def test_return_rejects_attempt_from_another_donation() -> None:
    first_donation = _create_donation()
    second_donation = _create_donation()
    attempt_id = _attach_attempt(first_donation, PaymentStatus.PROCESSING)

    response = client.get(
        f"/payment/return?donation_id={second_donation}&attempt_id={attempt_id}"
    )

    assert response.status_code == 400
    assert "confirmation link is invalid" in response.text
