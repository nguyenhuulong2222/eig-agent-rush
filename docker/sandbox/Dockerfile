# Sandbox image: headless Chromium (Playwright) + tesseract OCR. One fresh container per task.
# Runs as uid 10001, read-only rootfs, /tmp and /dev/shm on tmpfs, no env, no secrets,
# network = eig_sandbox_net (county-site only). Input: /job/in/task.json (read-only mount).
# Output: /job/out (its own volume subpath). See eig_agent/sandbox.py for the run spec.
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23

# Both evidence paths in one image: EIG_EVIDENCE_PATH=vision|ocr is a config switch, not a rebuild.
# Tagalog OCR data ships as Filipino (fil) in Ubuntu.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-vie \
      tesseract-ocr-spa tesseract-ocr-fil \
 && rm -rf /var/lib/apt/lists/*

# The image ships the browsers (/ms-playwright) but not the Python package; pin it to the same version.
RUN python3 -m pip install --no-cache-dir --break-system-packages playwright==1.64.0

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin eig
COPY runner.py /opt/eig/runner.py

ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python3", "/opt/eig/runner.py"]
