import hashlib
import hmac
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import app
from core.database import session_scope
from domain.models import Donation, LedgerEntry, PaymentAttempt, WebhookEvent
from domain.states import PaymentStatus
from services.hyperswitch import HyperswitchProvider
from services.payment_provider import PaymentProviderError, ProviderPayment


client = TestClient(app)


def _create_donation() -> str:
    response = client.post(
        "/review",
        data={
            "campaign_id": "equipment",
            "amount": "25",
            "donor_name": "Resilience Test",
            "donor_email": "resilience@example.com",
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
            hyperswitch_payment_id="pay_resilience",
            idempotency_key=donation.checkout_idempotency_key,
            checkout_url="https://sandbox.example/checkout",
            amount_cents=donation.intended_amount_cents,
            currency="USD",
            status=status.value,
        )
        donation.status = status.value
        session.add(attempt)
        session.flush()
        return attempt.id


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("", "valid donation amount"),
        ("abc", "valid donation amount"),
        ("NaN", "valid donation amount"),
        ("Infinity", "valid donation amount"),
        ("$25", "valid donation amount"),
        ("1,000", "valid donation amount"),
        ("4.99", "minimum donation"),
        ("-25", "minimum donation"),
        ("1000.01", "above $1,000"),
        ("25.999", "dollars and cents"),
    ],
)
def test_review_rejects_bad_amounts_without_creating_records(
    amount: str, message: str
) -> None:
    response = client.post(
        "/review",
        data={
            "campaign_id": "equipment",
            "amount": amount,
            "donor_name": "Bad Amount",
            "donor_email": "amount@example.com",
        },
    )

    assert response.status_code == 422
    assert message in response.text
    with session_scope() as session:
        assert session.scalar(select(func.count(Donation.id))) == 0


@pytest.mark.parametrize(
    ("name", "email", "message"),
    [
        ("", "donor@example.com", "Enter your name"),
        ("A", "donor@example.com", "Enter your name"),
        ("Valid Donor", "missing-at.example.com", "valid email"),
        ("Valid Donor", "donor @example.com", "valid email"),
        ("Valid Donor", "a" * 245 + "@example.com", "254 characters"),
        ("N" * 81, "donor@example.com", "80 characters"),
    ],
)
def test_review_rejects_bad_contact_fields(
    name: str, email: str, message: str
) -> None:
    response = client.post(
        "/review",
        data={
            "campaign_id": "equipment",
            "amount": "25",
            "donor_name": name,
            "donor_email": email,
        },
    )

    assert response.status_code == 422
    assert message in response.text


def test_review_rejects_missing_or_wrong_parameter_types() -> None:
    missing_amount = client.post(
        "/review",
        data={
            "campaign_id": "equipment",
            "donor_name": "Missing Amount",
            "donor_email": "missing@example.com",
        },
    )
    invalid_boolean = client.post(
        "/review",
        data={
            "campaign_id": "equipment",
            "amount": "25",
            "donor_name": "Wrong Boolean",
            "donor_email": "boolean@example.com",
            "anonymous": "definitely-not-a-boolean",
        },
    )

    assert missing_amount.status_code == 422
    assert invalid_boolean.status_code == 422
    with session_scope() as session:
        assert session.scalar(select(func.count(Donation.id))) == 0


def test_payment_rejects_unknown_donation_without_calling_provider(monkeypatch) -> None:
    called = False

    async def should_not_run(_self, _request):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(HyperswitchProvider, "create_payment", should_not_run)
    response = client.post(
        "/payments",
        data={"donation_id": "don_not_real"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "card was not charged" in response.text.lower()
    assert called is False
    with session_scope() as session:
        assert session.scalar(select(func.count(PaymentAttempt.id))) == 0


def test_provider_failure_becomes_safe_error_and_failed_attempt(monkeypatch) -> None:
    donation_id = _create_donation()

    async def fail_safely(_self, _request):
        raise PaymentProviderError("The sandbox payment service is unavailable.")

    monkeypatch.setattr(HyperswitchProvider, "create_payment", fail_safely)
    response = client.post(
        "/payments", data={"donation_id": donation_id}, follow_redirects=False
    )

    assert response.status_code == 502
    assert "sandbox payment service is unavailable" in response.text
    with session_scope() as session:
        attempt = session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.donation_id == donation_id)
        )
        donation = session.get(Donation, donation_id)
        assert attempt and attempt.status == PaymentStatus.FAILED.value
        assert donation and donation.status == PaymentStatus.FAILED.value


def test_duplicate_payment_posts_reuse_one_checkout(monkeypatch) -> None:
    donation_id = _create_donation()
    calls = 0

    async def create_once(_self, request):
        nonlocal calls
        calls += 1
        return ProviderPayment(
            payment_id="pay_single_checkout",
            status=PaymentStatus.REQUIRES_PAYMENT_METHOD,
            amount_cents=request.amount_cents,
            currency=request.currency,
            checkout_url="https://sandbox.example/one-checkout",
        )

    monkeypatch.setattr(HyperswitchProvider, "create_payment", create_once)
    first = client.post(
        "/payments", data={"donation_id": donation_id}, follow_redirects=False
    )
    second = client.post(
        "/payments", data={"donation_id": donation_id}, follow_redirects=False
    )

    assert first.status_code == 303
    assert second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert calls == 1
    with session_scope() as session:
        assert session.scalar(select(func.count(PaymentAttempt.id))) == 1


def test_retry_rejects_nonfinal_attempt_without_new_attempt() -> None:
    donation_id = _create_donation()
    attempt_id = _attach_attempt(donation_id, PaymentStatus.PROCESSING)

    response = client.post(
        f"/donations/{donation_id}/retry",
        data={"attempt_id": attempt_id},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "not eligible" in response.text
    with session_scope() as session:
        assert session.scalar(select(func.count(PaymentAttempt.id))) == 1


def test_tampered_verification_and_confirmation_ids_return_client_errors() -> None:
    verify = client.post(
        "/api/donations/don_fake/attempts/att_fake/verify"
    )
    confirmation = client.get(
        "/donations/don_fake/confirmation?attempt_id=att_fake"
    )
    payment_return = client.get(
        "/payment/return?donation_id=don_fake&attempt_id=att_fake"
    )

    assert verify.status_code == 400
    assert verify.json()["status"] == "verification_error"
    assert confirmation.status_code == 400
    assert payment_return.status_code == 400


def test_authoritative_amount_mismatch_never_completes_donation(monkeypatch) -> None:
    donation_id = _create_donation()
    attempt_id = _attach_attempt(donation_id, PaymentStatus.PROCESSING)

    async def retrieve_wrong_amount(_self, payment_id: str) -> ProviderPayment:
        return ProviderPayment(
            payment_id=payment_id,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=9999,
            currency="USD",
            safe_metadata={
                "donation_id": donation_id,
                "payment_attempt_id": attempt_id,
            },
        )

    monkeypatch.setattr(
        HyperswitchProvider, "retrieve_payment", retrieve_wrong_amount
    )
    response = client.post(
        f"/api/donations/{donation_id}/attempts/{attempt_id}/verify"
    )

    assert response.status_code == 400
    assert "amount does not match" in response.json()["message"]
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and donation.status == PaymentStatus.PROCESSING.value
        assert attempt and attempt.status == PaymentStatus.PROCESSING.value
        assert session.scalar(select(func.count(LedgerEntry.id))) == 0


@pytest.mark.parametrize("raw", [b"not-json", b"[]"])
def test_signed_malformed_webhook_returns_bad_request(
    monkeypatch, raw: bytes
) -> None:
    secret = "resilience-webhook-secret"
    monkeypatch.setenv("HYPERSWITCH_WEBHOOK_SECRET", secret)
    signature = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()

    response = client.post(
        "/webhooks/hyperswitch",
        content=raw,
        headers={"x-webhook-signature-512": signature},
    )

    assert response.status_code == 400
    assert response.json() == {"received": False, "error": "invalid_payload"}
    with session_scope() as session:
        assert session.scalar(select(func.count(WebhookEvent.event_id))) == 0


def test_signed_webhook_with_missing_required_fields_is_not_stored(monkeypatch) -> None:
    secret = "resilience-webhook-secret"
    monkeypatch.setenv("HYPERSWITCH_WEBHOOK_SECRET", secret)
    raw = json.dumps(
        {"event_type": "payment_succeeded", "content": {"object": {}}},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()

    response = client.post(
        "/webhooks/hyperswitch",
        content=raw,
        headers={"x-webhook-signature-512": signature},
    )

    assert response.status_code == 400
    assert "event ID" in response.json()["error"]
    with session_scope() as session:
        assert session.scalar(select(func.count(WebhookEvent.event_id))) == 0
