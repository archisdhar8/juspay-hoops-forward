import pytest

from models.donation import DonationValidationError, parse_donation


def test_parses_standard_donation() -> None:
    donation = parse_donation(
        campaign_id="scholarships",
        donor_name="  Archis   Dhar ",
        donor_email="ARCHIS@example.com",
        amount="50",
        anonymous=False,
        cover_fees=False,
    )

    assert donation.donor_name == "Archis Dhar"
    assert donation.donor_email == "archis@example.com"
    assert donation.gift_cents == 5000
    assert donation.total_cents == 5000
    assert donation.fee_cents == 0


def test_fee_coverage_grosses_up_total() -> None:
    donation = parse_donation(
        campaign_id="equipment",
        donor_name="A Donor",
        donor_email="a@example.com",
        amount="50",
        anonymous=False,
        cover_fees=True,
    )

    assert donation.total_cents == 5180
    assert donation.fee_cents == 180


@pytest.mark.parametrize("amount", ["0", "4.99", "1000.01", "abc"])
def test_rejects_invalid_amounts(amount: str) -> None:
    with pytest.raises(DonationValidationError):
        parse_donation(
            campaign_id="court-access",
            donor_name="A Donor",
            donor_email="a@example.com",
            amount=amount,
            anonymous=False,
            cover_fees=False,
        )


def test_anonymous_donor_can_omit_name() -> None:
    donation = parse_donation(
        campaign_id="court-access",
        donor_name="",
        donor_email="private@example.com",
        amount="25",
        anonymous=True,
        cover_fees=False,
    )

    assert donation.anonymous is True
    assert donation.donor_name == ""
