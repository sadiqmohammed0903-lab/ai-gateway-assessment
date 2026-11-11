"""Standard gateway error payload shared by every HTTP task."""

from pydantic import BaseModel


class ErrorBody(BaseModel):
    type: str
    code: str
    message: str
    request_id: str


class GatewayError(BaseModel):
    error: ErrorBody


def make_error(type_: str, code: str, message: str, request_id: str) -> GatewayError:
    return GatewayError(
        error=ErrorBody(type=type_, code=code, message=message, request_id=request_id)
    )
