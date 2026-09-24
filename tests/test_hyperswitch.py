from services.hyperswitch import _extract_checkout_url


def test_extracts_nested_payment_link() -> None:
    data = {"payment_link": {"link": "https://sandbox.example/checkout"}}
    assert _extract_checkout_url(data) == "https://sandbox.example/checkout"


def test_extracts_flat_payment_link() -> None:
    data = {"payment_link": "https://sandbox.example/checkout"}
    assert _extract_checkout_url(data) == "https://sandbox.example/checkout"
