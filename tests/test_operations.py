from datetime import timedelta

from fastapi.testclient import TestClient

from app import app
from core.database import session_scope
from domain.models import Donation, PaymentAttempt, PaymentEvent, WebhookEvent, utc_now
from domain.states import EventSource, PaymentStatus
from models.donation import parse_donation
from services.donation_service import create_donation_intent
from services.ledger_service import record_donation_succeeded


client = TestClient(app)


def _ops_auth() -> tuple[str, str]:
    return ("operator", "test-operations-token")


def _operations_donation() -> tuple[str, str]:
    value = parse_donation(
        campaign_id="court-access",
        donor_name="Operations Donor",
        donor_email="private-operations@example.com",
        amount="50",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, value)
        session.flush()
        attempt = PaymentAttempt(
            donation_id=donation.id,
            hyperswitch_payment_id="pay_operations",
            idempotency_key=donation.checkout_idempotency_key,
            amount_cents=5000,
            currency="USD",
            status=PaymentStatus.SUCCEEDED.value,
            confirmation_source=EventSource.WEBHOOK.value,
        )
        donation.status = PaymentStatus.SUCCEEDED.value
        session.add(attempt)
        session.flush()
        record_donation_succeeded(session, donation, attempt)
        session.add(
            PaymentEvent(
                donation_id=donation.id,
                payment_attempt_id=attempt.id,
                external_hyperswitch_id=attempt.hyperswitch_payment_id,
                event_type="donation_completed",
                source=EventSource.WEBHOOK.value,
                previous_state="processing",
                next_state="succeeded",
                safe_metadata={},
            )
        )
        return donation.id, attempt.id


def test_operations_requires_admin_credential(monkeypatch) -> None:
    monkeypatch.setenv("OPERATIONS_ADMIN_TOKEN", "test-operations-token")
    assert client.get("/operations").status_code == 401
    assert client.get("/operations", auth=("operator", "wrong")).status_code == 401


def test_operations_masks_pii_and_shows_ledger(monkeypatch) -> None:
    monkeypatch.setenv("OPERATIONS_ADMIN_TOKEN", "test-operations-token")
    donation_id, _ = _operations_donation()
    response = client.get("/operations", auth=_ops_auth())

    assert response.status_code == 200
    assert donation_id in response.text
    assert "$50.00" in response.text
    assert "confirmed by" not in response.text.lower() or "webhook" in response.text
    assert "private-operations@example.com" not in response.text
    assert "Operations Donor" not in response.text


def test_operations_detail_shows_timeline_without_email(monkeypatch) -> None:
    monkeypatch.setenv("OPERATIONS_ADMIN_TOKEN", "test-operations-token")
    donation_id, _ = _operations_donation()
    response = client.get(f"/operations/donations/{donation_id}", auth=_ops_auth())

    assert response.status_code == 200
    assert "Event timeline" in response.text
    assert "donation_completed" in response.text
    assert "donation_succeeded" in response.text
    assert "private-operations@example.com" not in response.text


def test_operations_flags_duplicate_and_stuck_processing(monkeypatch) -> None:
    monkeypatch.setenv("OPERATIONS_ADMIN_TOKEN", "test-operations-token")
    donation_id, attempt_id = _operations_donation()
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and attempt
        donation.status = PaymentStatus.PROCESSING.value
        attempt.status = PaymentStatus.PROCESSING.value
        attempt.updated_at = utc_now() - timedelta(minutes=20)
        session.add(
            WebhookEvent(
                event_id="evt_ops_duplicate",
                event_type="payment_processing",
                hyperswitch_payment_id=attempt.hyperswitch_payment_id,
                payload_hash="a" * 64,
                processing_status="processed",
                safe_metadata={},
                duplicate_count=1,
            )
        )
    response = client.get("/operations", auth=_ops_auth())
    assert "Duplicate webhook" in response.text
    assert "Payment stuck in processing" in response.text
