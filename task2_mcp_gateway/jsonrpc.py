"""JSON-RPC 2.0 envelope parsing and error helpers for the gateway."""

import json
import unicodedata
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000
UNAUTHORIZED_TOOL_CALL = -32001

ADMIN_TOOL_PREFIX = "admin_"


@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: Any
    id: Any
    has_id: bool


@dataclass(frozen=True)
class JsonRpcFailure:
    code: int
    message: str
    id: Any = None


def error_body(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def _valid_id(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, str)
        or (isinstance(value, int) and not isinstance(value, bool))
    )


def parse_request(body: bytes) -> JsonRpcRequest | JsonRpcFailure:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return JsonRpcFailure(PARSE_ERROR, "Parse error")
    if isinstance(payload, list):
        return JsonRpcFailure(INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(payload, dict):
        return JsonRpcFailure(INVALID_REQUEST, "Invalid Request")

    has_id = "id" in payload
    request_id = payload.get("id")
    if has_id and not _valid_id(request_id):
        return JsonRpcFailure(INVALID_REQUEST, "Invalid Request: bad id")
    echo_id = request_id if has_id else None

    if payload.get("jsonrpc") != "2.0":
        return JsonRpcFailure(INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'", echo_id)
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return JsonRpcFailure(INVALID_REQUEST, "Invalid Request: method must be a string", echo_id)
    params = payload.get("params")
    if "params" in payload and not isinstance(params, dict | list):
        return JsonRpcFailure(
            INVALID_REQUEST, "Invalid Request: params must be structured", echo_id
        )
    return JsonRpcRequest(method=method, params=params, id=request_id, has_id=has_id)


def extract_tool_name(params: Any) -> str | None:
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        return str(params["name"])
    return None


def normalise_tool_name(name: str) -> str:
    """Canonical form used only for the authorisation decision, never forwarded.

    NFKC folds full-width and compatibility characters; format characters (zero-width
    joiners etc.) are dropped; then trim and casefold.
    """
    folded = unicodedata.normalize("NFKC", name)
    visible = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return visible.strip().casefold()


def is_admin_tool(name: str) -> bool:
    return normalise_tool_name(name).startswith(ADMIN_TOOL_PREFIX)
