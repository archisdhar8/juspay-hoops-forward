import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import app
from core.database import session_scope
from domain.models import Donation, LedgerEntry, PaymentAttempt, PaymentEvent, WebhookEvent
from domain.states import LedgerEntryType, PaymentStatus
from models.donation import parse_donation
from services.donation_service import create_donation_intent
from services.hyperswitch import HyperswitchProvider
from services.payment_provider import ProviderPayment


client = TestClient(app)
WEBHOOK_SECRET = "rotated-test-webhook-secret"


def _persist_attempt() -> tuple[str, str]:
    donation_input = parse_donation(
        campaign_id="equipment",
        donor_name="Webhook Donor",
        donor_email="webhook@example.com",
        amount="25",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, donation_input)
        session.flush()
        attempt = PaymentAttempt(
            donation_id=donation.id,
            hyperswitch_payment_id="pay_webhook",
            idempotency_key=donation.checkout_idempotency_key,
            checkout_url="https://sandbox.example/checkout",
            amount_cents=2500,
            currency="USD",
            status=PaymentStatus.PROCESSING.value,
        )
        donation.status = PaymentStatus.PROCESSING.value
        session.add(attempt)
        session.flush()
        return donation.id, attempt.id


def _payload(
    *,
    event_id: str,
    event_type: str,
    donation_id: str,
    attempt_id: str,
    status: str = "succeeded",
    updated: str = "2026-09-24T20:00:00Z",
    extra: dict | None = None,
) -> dict:
    resource = {
        "payment_id": "pay_webhook",
        "status": status,
        "amount": 2500,
        "currency": "USD",
        "updated": updated,
        "metadata": {
            "donation_id": donation_id,
            "payment_attempt_id": attempt_id,
            "campaign_id": "equipment",
        },
    }
    resource.update(extra or {})
    return {
        "event_id": event_id,
        "event_type": event_type,
        "content": {"type": "payment_details", "object": resource},
    }


def _post_signed(monkeypatch, payload: dict):
    monkeypatch.setenv("HYPERSWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
    return client.post(
        "/webhooks/hyperswitch",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-webhook-signature-512": signature,
        },
    )


def test_rejects_invalid_webhook_signature(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    monkeypatch.setenv("HYPERSWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    response = client.post(
        "/webhooks/hyperswitch",
        json=_payload(
            event_id="evt_invalid",
            event_type="payment_succeeded",
            donation_id=donation_id,
            attempt_id=attempt_id,
        ),
        headers={"x-webhook-signature-512": "wrong"},
    )
    assert response.status_code == 401
    with session_scope() as session:
        assert session.get(WebhookEvent, "evt_invalid") is None


def test_webhook_completes_donation_without_browser_return(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    response = _post_signed(
        monkeypatch,
        _payload(
            event_id="evt_success",
            event_type="payment_succeeded",
            donation_id=donation_id,
            attempt_id=attempt_id,
        ),
    )
    assert response.status_code == 200
    assert response.json()["processing_status"] == "processed"
    with session_scope() as session:
        donation = session.get(Donation, donation_id)
        attempt = session.get(PaymentAttempt, attempt_id)
        assert donation and donation.status == PaymentStatus.SUCCEEDED.value
        assert attempt and attempt.confirmation_source == "webhook"
        entry = session.scalar(
            select(LedgerEntry).where(
                LedgerEntry.entry_type == LedgerEntryType.DONATION_SUCCEEDED.value
            )
        )
        assert entry and entry.amount_cents == 2500


def test_duplicate_webhook_is_idempotent(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    payload = _payload(
        event_id="evt_duplicate",
        event_type="payment_succeeded",
        donation_id=donation_id,
        attempt_id=attempt_id,
    )
    first = _post_signed(monkeypatch, payload)
    second = _post_signed(monkeypatch, payload)

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    with session_scope() as session:
        webhook = session.get(WebhookEvent, "evt_duplicate")
        assert webhook and webhook.duplicate_count == 1
        assert session.scalar(select(func.count(LedgerEntry.id))) == 1


def test_out_of_order_webhook_is_retained_without_regression(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    newest = _payload(
        event_id="evt_newest",
        event_type="payment_succeeded",
        donation_id=donation_id,
        attempt_id=attempt_id,
        updated="2026-09-24T21:00:00Z",
    )
    older = _payload(
        event_id="evt_older",
        event_type="payment_processing",
        donation_id=donation_id,
        attempt_id=attempt_id,
        status="processing",
        updated="2026-09-24T20:00:00Z",
    )
    assert _post_signed(monkeypatch, newest).status_code == 200
    response = _post_signed(monkeypatch, older)

    assert response.json()["processing_status"] == "ignored_out_of_order"
    with session_scope() as session:
        attempt = session.get(PaymentAttempt, attempt_id)
        webhook = session.get(WebhookEvent, "evt_older")
        assert attempt and attempt.status == PaymentStatus.SUCCEEDED.value
        assert webhook and webhook.processing_status == "ignored_out_of_order"


def test_newer_invalid_regression_is_retained_for_attention(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    _post_signed(
        monkeypatch,
        _payload(
            event_id="evt_final",
            event_type="payment_succeeded",
            donation_id=donation_id,
            attempt_id=attempt_id,
            updated="2026-09-24T20:00:00Z",
        ),
    )
    response = _post_signed(
        monkeypatch,
        _payload(
            event_id="evt_regression",
            event_type="payment_processing",
            donation_id=donation_id,
            attempt_id=attempt_id,
            status="processing",
            updated="2026-09-24T21:00:00Z",
        ),
    )
    assert response.json()["processing_status"] == "needs_attention"
    with session_scope() as session:
        webhook = session.get(WebhookEvent, "evt_regression")
        invalid = session.scalar(
            select(PaymentEvent).where(
                PaymentEvent.event_type == "invalid_state_transition"
            )
        )
        assert webhook and webhook.processing_status == "needs_attention"
        assert invalid is not None


def test_ambiguous_webhook_retrieves_authoritative_payment(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()

    async def retrieve(_self, payment_id: str) -> ProviderPayment:
        assert payment_id == "pay_webhook"
        return ProviderPayment(
            payment_id=payment_id,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=2500,
            currency="USD",
            safe_metadata={
                "donation_id": donation_id,
                "payment_attempt_id": attempt_id,
            },
        )

    monkeypatch.setattr(HyperswitchProvider, "retrieve_payment", retrieve)
    ambiguous = {
        "event_id": "evt_ambiguous",
        "event_type": "payment_succeeded",
        "content": {
            "type": "payment_details",
            "object": {
                "payment_id": "pay_webhook",
                "updated": "2026-09-24T20:00:00Z",
            },
        },
    }
    response = _post_signed(monkeypatch, ambiguous)
    assert response.status_code == 200
    assert response.json()["processing_status"] == "processed"


def test_refund_webhook_adds_single_negative_ledger_effect(monkeypatch) -> None:
    donation_id, attempt_id = _persist_attempt()
    _post_signed(
        monkeypatch,
        _payload(
            event_id="evt_paid",
            event_type="payment_succeeded",
            donation_id=donation_id,
            attempt_id=attempt_id,
        ),
    )
    refund = {
        "event_id": "evt_refund",
        "event_type": "refund_succeeded",
        "content": {
            "type": "refund_details",
            "object": {
                "refund_id": "ref_123",
                "payment_id": "pay_webhook",
                "amount": 1000,
                "updated": "2026-09-24T22:00:00Z",
            },
        },
    }
    first = _post_signed(monkeypatch, refund)
    second = _post_signed(monkeypatch, refund)
    assert first.json()["processing_status"] == "processed"
    assert second.json()["duplicate"] is True
    with session_scope() as session:
        refund_entries = session.scalars(
            select(LedgerEntry).where(
                LedgerEntry.entry_type == LedgerEntryType.REFUND_COMPLETED.value
            )
        ).all()
        attempt = session.get(PaymentAttempt, attempt_id)
        assert len(refund_entries) == 1
        assert refund_entries[0].amount_cents == -1000
        assert attempt and attempt.status == PaymentStatus.PARTIALLY_REFUNDED.value
