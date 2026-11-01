# Public web container: static UI + /api forwarder. Contains ONLY eig_agent/web.py and the static
# files: no orchestrator code, no openai SDK, no secrets, no Docker access.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY eig_agent/__init__.py eig_agent/web.py eig_agent/
COPY eig_agent/web/ eig_agent/web/

RUN useradd --system --uid 10003 --no-create-home web
USER web
EXPOSE 8000
CMD ["python", "-m", "eig_agent.web", "--port", "8000"]
