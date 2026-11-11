# Uses the project venv when present (Windows or POSIX), otherwise whatever `python` is on PATH.
PY := $(firstword $(wildcard .venv/Scripts/python.exe .venv/bin/python) python)
PKGS := common task1_mcp_server task2_mcp_gateway task3_llm_guardrail task4_model_router

.PHONY: test test-task1 test-task2 test-task3 test-task4 lint bench

test:
	$(PY) -m pytest

test-task1:
	$(PY) -m pytest task1_mcp_server

test-task2:
	$(PY) -m pytest task2_mcp_gateway

test-task3:
	$(PY) -m pytest task3_llm_guardrail

test-task4:
	$(PY) -m pytest task4_model_router

lint:
	$(PY) -m ruff check .
	$(PY) -m mypy $(PKGS)

bench:
	$(PY) -m task3_llm_guardrail.bench_ttft --runs 50 --delay-ms 20
