from fastapi.testclient import TestClient

from app import app
from services.hyperswitch import HyperswitchClient


client = TestClient(app)


def test_home_page_renders() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Hoops Forward" in response.text
    assert "Sandbox demonstration" in response.text


def test_review_recalculates_total_on_server() -> None:
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
    assert "api" not in response.text.lower()


def test_verified_success_renders_confirmation(monkeypatch) -> None:
    async def retrieve_success(_self, payment_id: str) -> dict:
        assert payment_id == "pay_success"
        return {
            "status": "succeeded",
            "metadata": {
                "donation_reference": "HF-SUCCESS",
                "campaign_id": "scholarships",
            },
        }

    monkeypatch.setattr(HyperswitchClient, "retrieve_payment", retrieve_success)
    response = client.get("/payment/return?payment_id=pay_success")

    assert response.status_code == 200
    assert "Donation confirmed" in response.text
    assert "HF-SUCCESS" in response.text
    assert "Succeeded" in response.text


def test_verified_failure_renders_retry_message(monkeypatch) -> None:
    async def retrieve_failure(_self, payment_id: str) -> dict:
        assert payment_id == "pay_failed"
        return {
            "status": "failed",
            "metadata": {
                "donation_reference": "HF-FAILED",
                "campaign_id": "equipment",
            },
        }

    monkeypatch.setattr(HyperswitchClient, "retrieve_payment", retrieve_failure)
    response = client.get("/payment/return?payment_id=pay_failed")

    assert response.status_code == 200
    assert "payment didn’t go through" in response.text
    assert "HF-FAILED" in response.text
    assert "Try the donation again" in response.text
