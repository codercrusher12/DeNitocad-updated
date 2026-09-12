# Pinned Python 3.11 - OCP's pip wheels only cover 3.9-3.12 (confirmed
# against PyPI/CadQuery docs as of 2026-07-31), and Railway's Nixpacks
# builder has repeatedly failed to honor .python-version/runtime.txt
# pins in practice (multiple open reports on Railway's own help forum),
# defaulting to whatever its current Nix snapshot ships. A Dockerfile
# sidesteps that guesswork entirely - this is the fix for the
# Railway/Vercel deployment crashes you were hitting before.
FROM python:3.11-slim

WORKDIR /app

# libGL.so.1 and friends: OCP's wheel links against OpenGL/X11 libraries
# that python:3.11-slim doesn't ship. Without these, `import cadquery`
# fails at import time with "ImportError: libGL.so.1: cannot open shared
# object file" - a known, repeatedly-reported CadQuery/Docker issue.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libxrender1 \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a dedicated, unprivileged user rather than root - CadQuery/OCCT
# don't need root, and running as root in a container is a needless
# privilege-escalation surface if the process is ever compromised via a
# dependency vuln. Output dir is created and owned by that user up front
# so /generate's first write doesn't hit a permissions error.
#
# /data is pre-created and chowned here too, not just /app/output: when
# docker-compose.yml mounts a *named* volume at /data on a fresh volume,
# Docker seeds it from whatever already exists at that path in the image
# layer (including ownership) - so if this directory didn't exist yet, or
# was still root-owned, the app's first write to the mounted volume would
# fail with a permissions error despite USER nitocad below being correct
# for everywhere else in the image.
RUN useradd --create-home --uid 1000 nitocad \
    && mkdir -p /app/output /data \
    && chown -R nitocad:nitocad /app /data
USER nitocad

# Railway's ephemeral filesystem wipes ./output and the sqlite file on
# every redeploy/restart - see storage.py and db.py's DB_PATH note.
# Mount a persistent volume at /data and set DB_PATH=/data/nl_to_cad.db
# (and configure R2 for generated files) before relying on this in
# production.
ENV PORT=8000
ENV ENVIRONMENT=production
ENV LOG_FORMAT=json

# Without this, Python fully buffers stdout when it isn't attached to a
# real terminal - which is exactly what Railway's log capture looks like
# to the process. uvicorn's own INFO lines still show up because its
# logging handlers flush explicitly, but any plain print() (e.g. the
# startup diagnostics in a2mcp/x402.py's build_paid_app()) can sit in the
# buffer indefinitely on a long-running server that never exits, so it
# never reaches the Railway deploy log even though the code ran. This is
# the fix for that, not a code change to build_paid_app() itself.
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Container-level liveness check hitting the same /healthz route the
# orchestrator's own probe would use - lets `docker ps` and
# `docker inspect --format='{{.State.Health.Status}}'` show a failing
# instance locally, before it ever reaches Railway's own health checking.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz', timeout=3)" || exit 1

# --proxy-headers + --forwarded-allow-ips="*": Railway terminates TLS at
# its edge (railway-hikari) and forwards plain HTTP to this container.
# Without these flags uvicorn takes the raw connection's scheme (http) as
# truth instead of reading X-Forwarded-Proto, so any URL it builds itself -
# e.g. Starlette's trailing-slash redirect on POST /mcp -> /mcp/ - comes
# back as an http:// Location header even though the deploy is https-only.
# "*" is safe here specifically because Railway's own edge is the only
# thing that can reach this container's PORT (see .dockerignore/network
# posture in README) - it is not exposed directly to the open internet.
CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=*"]
