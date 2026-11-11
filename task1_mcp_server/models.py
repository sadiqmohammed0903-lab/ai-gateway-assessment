"""Strict input models for the two tools. Their JSON schemas are published in tools/list."""

from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

# ASCII digits only: `\d` would also accept other Unicode digits.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
MAX_REFUND_AMOUNT = 10_000.00
MIN_REASON_LENGTH = 10
MAX_REASON_LENGTH = 500

CustomerId = Annotated[
    str,
    StringConstraints(pattern=CUSTOMER_ID_PATTERN),
    Field(description="Customer identifier in the form CUST-XXXXX (five digits)."),
]


class StrictInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordInput(StrictInput):
    customer_id: CustomerId


class TriggerRefundInput(StrictInput):
    customer_id: CustomerId
    amount: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_REFUND_AMOUNT,
            allow_inf_nan=False,
            description="Refund amount, positive, at most 2 decimal places.",
        ),
    ]
    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=MIN_REASON_LENGTH, max_length=MAX_REASON_LENGTH
        ),
        Field(description="Reason for the refund, at least 10 characters after trimming."),
    ]

    @field_validator("amount")
    @classmethod
    def two_decimals(cls, value: float) -> float:
        if Decimal(str(value)).as_tuple().exponent < -2:  # type: ignore[operator]
            raise ValueError("amount must have at most 2 decimal places")
        return value


def format_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Field/message pairs only; rejected values are never echoed back."""
    return [
        {"field": ".".join(str(p) for p in err["loc"]) or "arguments", "message": err["msg"]}
        for err in exc.errors(include_input=False, include_url=False)
    ]
