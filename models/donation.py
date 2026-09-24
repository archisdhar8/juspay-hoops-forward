from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re


CAMPAIGNS = {
    "equipment": {
        "name": "Equipment that lasts",
        "short_name": "Equipment",
        "description": "Balls, uniforms, and training gear for community programs.",
    },
    "scholarships": {
        "name": "Play without barriers",
        "short_name": "Scholarships",
        "description": "Registration and travel scholarships for young athletes.",
    },
    "court-access": {
        "name": "Open more courts",
        "short_name": "Court access",
        "description": "Safe gym time and neighborhood court restoration.",
    },
}

MINIMUM_DONATION_CENTS = 500
MAXIMUM_DONATION_CENTS = 100_000
CARD_RATE = Decimal("0.029")
CARD_FIXED_FEE_CENTS = 30


class DonationValidationError(ValueError):
    """Raised when donor-controlled form values are invalid."""


@dataclass(frozen=True)
class Donation:
    campaign_id: str
    donor_name: str
    donor_email: str
    anonymous: bool
    cover_fees: bool
    gift_cents: int
    total_cents: int
    fee_cents: int

    @property
    def campaign(self) -> dict[str, str]:
        return CAMPAIGNS[self.campaign_id]

    @property
    def gift_display(self) -> str:
        return format_usd(self.gift_cents)

    @property
    def fee_display(self) -> str:
        return format_usd(self.fee_cents)

    @property
    def total_display(self) -> str:
        return format_usd(self.total_cents)


def parse_donation(
    *,
    campaign_id: str,
    donor_name: str,
    donor_email: str,
    amount: str,
    anonymous: bool,
    cover_fees: bool,
) -> Donation:
    if campaign_id not in CAMPAIGNS:
        raise DonationValidationError("Choose a valid campaign.")

    clean_name = " ".join(donor_name.split())
    clean_email = donor_email.strip().lower()

    if not anonymous and len(clean_name) < 2:
        raise DonationValidationError("Enter your name or choose anonymous donation.")
    if len(clean_name) > 80:
        raise DonationValidationError("Name must be 80 characters or fewer.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean_email):
        raise DonationValidationError("Enter a valid email address.")

    try:
        dollars = Decimal(amount.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise DonationValidationError("Enter a valid donation amount.") from exc

    gift_cents = int(dollars * 100)
    if gift_cents < MINIMUM_DONATION_CENTS:
        raise DonationValidationError("The minimum donation is $5.00.")
    if gift_cents > MAXIMUM_DONATION_CENTS:
        raise DonationValidationError("For gifts above $1,000, please contact our team.")

    total_cents = gift_cents
    fee_cents = 0
    if cover_fees:
        gross_cents = (
            Decimal(gift_cents + CARD_FIXED_FEE_CENTS)
            / (Decimal("1") - CARD_RATE)
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        total_cents = int(gross_cents)
        fee_cents = total_cents - gift_cents

    return Donation(
        campaign_id=campaign_id,
        donor_name=clean_name,
        donor_email=clean_email,
        anonymous=anonymous,
        cover_fees=cover_fees,
        gift_cents=gift_cents,
        total_cents=total_cents,
        fee_cents=fee_cents,
    )


def format_usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
