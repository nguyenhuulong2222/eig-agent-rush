# Orchestrator + web UI.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EIG_USAGE_LOG=/tmp/usage.jsonl
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY checker/ checker/
COPY eig_agent/ eig_agent/
COPY config/ config/

RUN useradd --system --uid 10001 --no-create-home eig
USER eig
EXPOSE 8000
CMD ["python", "-m", "eig_agent", "serve", "--port", "8000"]
