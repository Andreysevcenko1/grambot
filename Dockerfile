FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/grambot.db \
    HEARTBEAT_PATH=/data/grambot.heartbeat

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grambot ./grambot
COPY main.py .

RUN useradd --create-home --uid 1000 grambot \
    && mkdir -p /data && chown grambot:grambot /data
USER grambot
VOLUME ["/data"]

# Docker (and compose/Swarm) mark the container unhealthy when the bot's
# heartbeat file goes stale; the in-process watchdog normally restarts the
# bot itself long before that.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "main.py", "--healthcheck"]

CMD ["python", "main.py"]
