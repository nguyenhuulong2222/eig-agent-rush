# Sandbox image: headless Chromium via Playwright. Placeholder for M3.
# M3 pins the exact image tag, adds the runner script, and verifies Chromium under
# --read-only / --tmpfs /tmp / --memory 1g / --cap-drop ALL as a non-root user.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

USER pwuser
WORKDIR /home/pwuser
CMD ["python", "-c", "print('eig sandbox placeholder (M3)')"]
