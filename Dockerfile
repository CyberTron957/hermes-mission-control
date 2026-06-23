# Hermes Swarm — self-contained image (Python + Hermes + Chromium + dashboard).
# Uses Debian Bookworm (stable) to avoid repo signature issues with Trixie.
FROM python:3.12-slim-bookworm

# System deps: git for VCS, curl for healthchecks, Chromium deps for browser tools.
# Pre-install the libraries Playwright/Chromium needs so the build is resilient
# even if Playwright's own --with-deps has repo issues.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        # Chromium system dependencies (Playwright --with-deps installs these,
        # but we pre-install to avoid failures from repo signing issues)
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install the swarm + its deps (pulls hermes-agent[all]).
RUN pip install --no-cache-dir .

# Chromium for the browser-publishing tools.
# First try Playwright's bundled Chromium with pre-installed deps.
# If that fails, try system chromium-browser. If all fails, warn but continue.
RUN python -m playwright install --with-deps chromium \
    || (apt-get update && apt-get install -y --no-install-recommends chromium \
        && rm -rf /var/lib/apt/lists/*) \
    || echo "WARN: Chromium install failed — browser tools will be unavailable"

# Persistent writable state (configs, queues, agent workspaces, monitoring db).
ENV SWARM_DATA_DIR=/data \
    SWARM_HOST=0.0.0.0 \
    SWARM_PORT=8000
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["hermes-swarm", "up"]
