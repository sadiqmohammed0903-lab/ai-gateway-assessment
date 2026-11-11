import math
from typing import Any

import pytest
from pydantic import ValidationError

from task1_mcp_server.models import (
    CUSTOMER_ID_PATTERN,
    GetCustomerRecordInput,
    TriggerRefundInput,
    format_errors,
)

GOOD_REASON = "Item arrived damaged"


@pytest.mark.parametrize("cid", ["CUST-12345", "CUST-00000", "CUST-99999"])
def test_valid_customer_ids(cid: str) -> None:
    assert GetCustomerRecordInput(customer_id=cid).customer_id == cid


@pytest.mark.parametrize(
    "cid",
    [
        "CUST-1234",
        "CUST-123456",
        "cust-12345",
        " CUST-12345",
        "CUST-12345 ",
        "CUST-12345\n",
        "CUST-１２３４５",
        "",
        "CUST-ABCDE",
        None,
        12345,
        ["CUST-12345"],
        True,
    ],
)
def test_invalid_customer_ids(cid: Any) -> None:
    with pytest.raises(ValidationError):
        GetCustomerRecordInput(customer_id=cid)


def test_missing_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GetCustomerRecordInput.model_validate({})
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": "CUST-12345", "admin": True})
    assert format_errors(exc.value)[0]["field"] == "admin"


def refund(**overrides: Any) -> TriggerRefundInput:
    payload = {"customer_id": "CUST-10001", "amount": 10.5, "reason": GOOD_REASON} | overrides
    return TriggerRefundInput.model_validate(payload)


@pytest.mark.parametrize("amount", [0.01, 1, 10.5, 99.99, 10_000.0])
def test_valid_amounts(amount: Any) -> None:
    assert refund(amount=amount).amount == amount


@pytest.mark.parametrize(
    "amount",
    [
        0,
        0.0,
        -1,
        -0.01,
        math.nan,
        math.inf,
        -math.inf,
        True,
        False,
        "10.5",
        "abc",
        None,
        10_000.01,
        1.005,
        0.001,
    ],
)
def test_invalid_amounts(amount: Any) -> None:
    with pytest.raises(ValidationError):
        refund(amount=amount)


def test_reason_length_rules() -> None:
    assert refund(reason="ten chars!").reason == "ten chars!"
    assert refund(reason="   padded reason   ").reason == "padded reason"
    for bad in ["123456789", "         x         ", "", "          ", 12345678901, None]:
        with pytest.raises(ValidationError):
            refund(reason=bad)
    with pytest.raises(ValidationError):
        refund(reason="x" * 501)


def test_error_format_never_echoes_input() -> None:
    with pytest.raises(ValidationError) as exc:
        refund(reason="short", amount="secret-value")
    rendered = str(format_errors(exc.value))
    assert "secret-value" not in rendered
    assert {e["field"] for e in format_errors(exc.value)} == {"reason", "amount"}


def test_schema_reflects_rules() -> None:
    props = TriggerRefundInput.model_json_schema()["properties"]
    assert props["customer_id"]["pattern"] == CUSTOMER_ID_PATTERN
    assert props["amount"]["exclusiveMinimum"] == 0
    assert props["amount"]["maximum"] == 10_000.0
    assert props["reason"]["minLength"] == 10
    assert TriggerRefundInput.model_json_schema()["additionalProperties"] is False
