# Setup guide (run this project on your own laptop)

This project has four small Python services (an MCP server, an MCP security gateway, an LLM streaming
guardrail, and a rate-limiting model router). Everything uses mock upstreams, so **no API keys, no
accounts and no paid services are needed**. It was developed and tested on Windows 11 with Python 3.14.

## 1. What you need first

| Tool | Version | Check with |
|---|---|---|
| Python | 3.11 or newer | `python --version` |
| Internet (first run only) | to download packages | |
| Node.js (optional) | any recent | `node --version` (only for MCP Inspector) |

Python download: https://www.python.org/downloads/ (on Windows tick **"Add python.exe to PATH"**).

## 2. Unzip and open a terminal in the project folder

Unzip the file. You should get a folder containing `pyproject.toml`, `README.md` and the
`task1_mcp_server`, `task2_mcp_gateway`, `task3_llm_guardrail`, `task4_model_router` folders.
Open a terminal **inside that folder**. Every command below must be run from there.

- Windows (PowerShell): right-click the folder > "Open in Terminal".
- macOS / Linux: `cd` into the folder.

## 3. Create the virtual environment and install

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```
macOS / Linux:
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```
This takes 1 to 3 minutes. If PowerShell says scripts are disabled, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

Then activate it (the prompt will start with `(.venv)`):
```powershell
.venv\Scripts\activate        # Windows
```
```bash
source .venv/bin/activate     # macOS / Linux
```
Open every new terminal in the project folder and activate the venv again.

## 4. Check that it works (automated tests)

```bash
python -m pytest
```
Expected last line: **`248 passed`** (about 40 seconds). The first run may be a few seconds slower because
the token counter downloads its vocabulary; if you are offline it automatically falls back to an estimate.

Run one task only: `python -m pytest task1_mcp_server` (also `task2_mcp_gateway`, `task3_llm_guardrail`,
`task4_model_router`).

Optional code-quality checks: `python -m ruff check .` and
`python -m mypy common task1_mcp_server task2_mcp_gateway task3_llm_guardrail task4_model_router`
(both should report no problems).

## 5. Try it by hand

### Task 1: MCP server (talks over stdio, not HTTP)
```bash
python try_task1.py
```
It starts the server and calls both tools with good and bad input. You should see `-32602` errors for bad
input and `isError` results for "not found" / "refund exceeds balance".

Optional, with Node.js installed (opens a web UI to click through the tools):
```bash
npx @modelcontextprotocol/inspector python -m task1_mcp_server.server
```

### Tasks 2 to 4: start all services (Windows, one command)
```powershell
.\start.ps1      # starts 7 services in the background, prints OK for each
.\demo.ps1       # runs a demo of all three gateways
.\stop.ps1       # stops everything
```
Expected from `demo.ps1`:
- a viewer calling `admin_reset_key` gets `-32001 Unauthorized Tool Call`; an admin succeeds
- streamed text where the email, SSN and card number appear as `[REDACTED]`
- a request served by `primary`; with the primary forced slow it is served by `secondary` after about 3 s;
  big requests then get `429` with a `retry-after` header

If a service fails to start, open `.run\<service-name>.err.log`.

### Tasks 2 to 4 on macOS / Linux (manual, one terminal per line)
```bash
# Task 2
python -m uvicorn task2_mcp_gateway.mock_downstream:app --port 9001
JWT_SECRET=demo-secret-demo-secret-demo-secret-123 DOWNSTREAM_URL=http://127.0.0.1:9001/mcp python -m task2_mcp_gateway.app
# Task 3
MOCK_LLM_DELAY_MS=300 python -m task3_llm_guardrail.mock_llm
python -m task3_llm_guardrail.app
# Task 4
python -m uvicorn task4_model_router.mock_providers:primary_app --port 9301
python -m uvicorn task4_model_router.mock_providers:secondary_app --port 9302
python -m task4_model_router.app
```
Then use the curl examples in `README.md`. (The `.ps1` scripts are Windows only; the commands above were
not run on macOS/Linux by the author.)

### Ports used
9000 MCP gateway, 9001 mock MCP server, 9100 LLM gateway, 9101 mock LLM, 9300 router, 9301 / 9302 mock providers.
Opening a service in a browser at `/` shows a 404; that is normal because the endpoints only accept POST.
Open `http://127.0.0.1:9000/docs` (or 9100, 9300) to see the API page of a running service.

## 6. Common problems

| Message | Cause and fix |
|---|---|
| `No module named 'task1_mcp_server'` | Wrong folder or venv not active. `cd` into the project folder and activate `.venv`. |
| `does not appear to be a Python project` | You are not in the folder that contains `pyproject.toml`. |
| `npx is not recognized` | Node.js is not installed. Only needed for MCP Inspector; skip it or install Node. |
| `running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. |
| `address already in use` | A service from an earlier run is still going. Run `.\stop.ps1` or close that terminal. |

## 7. Where to read more

- `README.md`: overview, architecture diagram, run commands for each task
- `task1_mcp_server/NOTES.md` ... `task4_model_router/NOTES.md`: design decisions and limitations per task
