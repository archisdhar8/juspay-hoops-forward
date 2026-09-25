import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.database import seed_reference_data, session_scope
from domain.models import AuditEvent, Donation, PaymentAttempt
from models.donation import parse_donation
from services.donation_service import create_donation_intent


def _donation_id() -> str:
    value = parse_donation(
        campaign_id="court-access",
        donor_name="Constraint Test",
        donor_email="constraint@example.com",
        amount="30",
        anonymous=False,
        cover_fees=False,
    )
    with session_scope() as session:
        donation = create_donation_intent(session, value)
        session.commit()
        return donation.id


def test_seeded_organization_and_campaigns_are_audited() -> None:
    with session_scope() as session:
        seed_reference_data(session)
        session.commit()
        actions = {event.action for event in session.scalars(select(AuditEvent))}
        audit_count = len(list(session.scalars(select(AuditEvent))))

    assert "demo_organization_created" in actions
    assert "demo_campaign_created" in actions
    assert audit_count == 4


def test_hyperswitch_payment_id_is_unique() -> None:
    donation_id = _donation_id()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            donation = session.get(Donation, donation_id)
            assert donation
            session.add_all(
                [
                    PaymentAttempt(
                        donation_id=donation.id,
                        hyperswitch_payment_id="pay_same",
                        idempotency_key="idem_first",
                        amount_cents=3000,
                        currency="USD",
                    ),
                    PaymentAttempt(
                        donation_id=donation.id,
                        hyperswitch_payment_id="pay_same",
                        idempotency_key="idem_second",
                        amount_cents=3000,
                        currency="USD",
                    ),
                ]
            )
