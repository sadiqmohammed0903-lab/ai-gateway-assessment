FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app
COPY pyproject.toml README.md ./
COPY common ./common
COPY task1_mcp_server ./task1_mcp_server
COPY task2_mcp_gateway ./task2_mcp_gateway
COPY task3_llm_guardrail ./task3_llm_guardrail
COPY task4_model_router ./task4_model_router
RUN pip install . \
 && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
 && useradd --create-home app && mkdir -p /data && chown app /data /opt/tiktoken-cache
USER app

# Default command is overridden per service in docker-compose.yml
CMD ["python", "-m", "task4_model_router.app"]
