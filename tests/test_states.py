import pytest

from domain.states import (
    InvalidStateTransition,
    PaymentStatus,
    UnknownPaymentStatus,
    ensure_payment_transition,
    map_provider_status,
)


@pytest.mark.parametrize(
    ("previous", "next_status"),
    [
        (PaymentStatus.CREATED, PaymentStatus.REQUIRES_PAYMENT_METHOD),
        (PaymentStatus.REQUIRES_PAYMENT_METHOD, PaymentStatus.PROCESSING),
        (PaymentStatus.PROCESSING, PaymentStatus.SUCCEEDED),
        (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED),
        (PaymentStatus.SUCCEEDED, PaymentStatus.DISPUTED),
    ],
)
def test_allows_deliberate_payment_transitions(previous, next_status) -> None:
    ensure_payment_transition(previous, next_status)


@pytest.mark.parametrize(
    ("previous", "next_status"),
    [
        (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING),
        (PaymentStatus.FAILED, PaymentStatus.SUCCEEDED),
        (PaymentStatus.REFUNDED, PaymentStatus.SUCCEEDED),
    ],
)
def test_rejects_invalid_payment_transitions(previous, next_status) -> None:
    with pytest.raises(InvalidStateTransition):
        ensure_payment_transition(previous, next_status)


def test_rejects_unknown_provider_status() -> None:
    with pytest.raises(UnknownPaymentStatus):
        map_provider_status("mystery_state")
