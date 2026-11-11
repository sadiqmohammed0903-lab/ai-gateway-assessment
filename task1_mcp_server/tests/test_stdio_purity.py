import json
import subprocess
import sys
import threading
from typing import Any

PROTOCOL = "2025-06-18"
EXPECTED_IDS = {1, 2, 3, 4, 5}


def _requests() -> list[dict[str, Any]]:
    def call(id_: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "get_customer_record", {"customer_id": "CUST-10001"}),
        call(4, "get_customer_record", {"customer_id": "bad"}),
        call(5, "nope", {}),
    ]


def run_raw(module: str) -> tuple[str, str]:
    """Drive the server over raw pipes; keep stdin open until every response has arrived."""
    proc = subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []
    seen = threading.Event()

    def read_stdout() -> None:
        assert proc.stdout
        for line in proc.stdout:
            stdout_lines.append(line)
            ids = {json.loads(ln).get("id") for ln in stdout_lines if ln.strip().startswith("{")}
            if EXPECTED_IDS <= ids:
                seen.set()

    def read_stderr() -> None:
        assert proc.stderr
        stderr_chunks.append(proc.stderr.read())

    readers = [threading.Thread(target=read_stdout), threading.Thread(target=read_stderr)]
    for t in readers:
        t.start()
    for msg in _requests():
        proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.write("{this is not json\n")
    proc.stdin.flush()
    seen.wait(timeout=20)
    proc.stdin.close()
    proc.wait(timeout=20)
    for t in readers:
        t.join(timeout=10)
    return "".join(stdout_lines), "".join(stderr_chunks)


def assert_pure_jsonrpc(stdout: str) -> list[dict[str, Any]]:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "server produced no stdout"
    parsed = []
    for line in lines:
        msg = json.loads(line)
        assert msg["jsonrpc"] == "2.0", line
        parsed.append(msg)
    return parsed


def test_stdout_is_pure_jsonrpc_and_logs_go_to_stderr() -> None:
    stdout, stderr = run_raw("task1_mcp_server.server")
    msgs = assert_pure_jsonrpc(stdout)
    by_id = {m["id"]: m for m in msgs if m.get("id") is not None}
    assert "result" in by_id[1] and "result" in by_id[2] and "result" in by_id[3]
    assert by_id[4]["error"]["code"] == -32602
    assert by_id[5]["error"]["code"] == -32602
    assert "starting customer-support-server" in stderr
    assert "invalid arguments for get_customer_record" in stderr


def test_stray_prints_never_reach_stdout() -> None:
    stdout, stderr = run_raw("task1_mcp_server.tests.stray_stdout_server")
    assert_pure_jsonrpc(stdout)
    assert "STRAY" not in stdout
    assert "STRAY PRINT" in stderr
