from fastapi.testclient import TestClient

from app import app


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
