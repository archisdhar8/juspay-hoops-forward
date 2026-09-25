from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from domain.models import (
    AuditEvent,
    Campaign,
    DEFAULT_ORGANIZATION_ID,
    Donation,
    DonorContact,
    Organization,
    PaymentEvent,
)
from domain.states import EventSource, PaymentStatus
from models.donation import Donation as DonationInput


class DonationNotFoundError(LookupError):
    pass


def create_donation_intent(session: Session, donation_input: DonationInput) -> Donation:
    campaign = session.get(Campaign, donation_input.campaign_id)
    organization = session.get(Organization, DEFAULT_ORGANIZATION_ID)
    if campaign is None or organization is None or not campaign.active:
        raise DonationNotFoundError("The selected campaign is unavailable.")
    donor_contact = DonorContact(
        name=donation_input.donor_name,
        email=donation_input.donor_email,
    )
    donation = Donation(
        organization=organization,
        campaign=campaign,
        donor_contact=donor_contact,
        intended_amount_cents=donation_input.total_cents,
        contribution_amount_cents=donation_input.gift_cents,
        fee_coverage_cents=donation_input.fee_cents,
        currency="USD",
        designation=donation_input.campaign["short_name"],
        anonymous_publicly=donation_input.anonymous,
        status=PaymentStatus.CREATED.value,
        checkout_idempotency_key=f"idem_{secrets.token_urlsafe(24)}",
    )
    session.add_all([donor_contact, donation])
    session.flush()
    session.add_all(
        [
            PaymentEvent(
                donation_id=donation.id,
                event_type="donation_created",
                source=EventSource.APPLICATION.value,
                previous_state=None,
                next_state=PaymentStatus.CREATED.value,
                safe_metadata={
                    "campaign_id": donation.campaign_id,
                    "amount_cents": donation.intended_amount_cents,
                    "currency": donation.currency,
                    "anonymous_publicly": donation.anonymous_publicly,
                },
            ),
            AuditEvent(
                entity_type="donation",
                entity_id=donation.id,
                action="created",
                actor_type="donor",
                safe_metadata={"campaign_id": donation.campaign_id},
            ),
        ]
    )
    session.flush()
    return donation


def get_donation(session: Session, donation_id: str) -> Donation:
    donation = session.get(Donation, donation_id)
    if donation is None:
        raise DonationNotFoundError("The donation could not be found.")
    return donation
