# Orchestrator: private API, holds the Vultr Inference key, talks to Docker only via socket-proxy.
# The test service runs the unit tests in this same image (python:3.12-slim, as in production).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EIG_USAGE_LOG=/var/eig/state/usage.jsonl \
    EIG_LEDGER_PATH=/var/eig/ledger/ledger.jsonl
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY checker/ checker/
COPY eig_agent/ eig_agent/
COPY config/ config/
COPY public/ public/
COPY results/ results/
COPY scripts/ scripts/

RUN useradd --system --uid 10001 --no-create-home eig \
 && mkdir -p /var/eig/state /var/eig/ledger /var/eig/county-data /var/eig/jobs \
 && chown -R eig /var/eig
USER eig
EXPOSE 8001
CMD ["python", "-m", "eig_agent", "orchestrator", "--port", "8001"]
