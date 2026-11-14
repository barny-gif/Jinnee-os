FROM python:3.12-slim
# Node 22 from NodeSource, as install.sh does: Claude Code runs on it, and the taste skill's scripts need 22 or newer.
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code
WORKDIR /app
COPY requirements.txt .
# Playwright and its Chromium with the system libraries it needs (tools/visual_check.py: page screenshots for the design check)
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
# The skills' tools send no usage data (the bridge sets these for every run too); the Impeccable engine lives in the image.
ENV DO_NOT_TRACK=1 IMPECCABLE_NO_TELEMETRY=1 IMPECCABLE_HOME=/opt/impeccable CHROME_PATH=/usr/local/bin/chromium
COPY . .
# The taste skill at its pinned commit and the Impeccable engine (sha256-checked). The browser must start, or the build stops here.
RUN { python tools/skills_fetch.py sync || echo "skills: not all fetched; later in the container: python tools/skills_fetch.py sync"; } \
 && ln -sf "$(python tools/visual_check.py --self-check | sed -n 's/^ok //p')" /usr/local/bin/chromium && test -x /usr/local/bin/chromium
CMD ["bash", "-c", "python core/jinnee.py & python connectors/ecom_sync.py loop & python dashboard/app.py"]
