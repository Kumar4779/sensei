# Talk to Sensei (Demo Slice 2): the hosted demo server, with its core and voice dependencies.
#
# Build from the repository root (the image needs packages/core and packages/voice too):
#     fly deploy . --config packages/demo/fly.toml --dockerfile packages/demo/Dockerfile
#
# No secret is in this image. DEEPGRAM_API_KEY, ELEVENLABS_API_KEY and SENSEI_DEMO_TOKEN
# reach the running machine only as Fly secrets (environment variables).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY packages/core /app/packages/core
COPY packages/voice /app/packages/voice
COPY packages/demo /app/packages/demo
RUN pip install /app/packages/core /app/packages/voice /app/packages/demo \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sensei \
    && install -m 0755 /app/packages/demo/entrypoint.sh /usr/local/bin/sensei-demo-entrypoint

# Inside the Fly machine the server listens on all interfaces; only Fly's proxy reaches it.
ENV SENSEI_DEMO_ALLOW_LAN=1 \
    SENSEI_DEMO_PORT=8080
EXPOSE 8080

# The entrypoint gives the /data volume (the daily spend counter) to the unprivileged
# user, then drops to it. Traces (visitors' transcripts) stay on the machine's
# ephemeral disk and are gone when it stops; no audio is ever written.
ENTRYPOINT ["/usr/local/bin/sensei-demo-entrypoint"]
CMD ["python", "-m", "sensei_demo", "--keys", "env", "--state-dir", "/data", \
     "--trace-dir", "/tmp/sensei-demo-traces", "--trusted-ip-header", "Fly-Client-IP", \
     "--daily-usd", "5", "--max-usd", "1", "--max-sessions", "8"]
