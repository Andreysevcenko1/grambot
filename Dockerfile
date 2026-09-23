FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/grambot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grambot ./grambot
COPY main.py .

RUN useradd --create-home --uid 1000 grambot \
    && mkdir -p /data && chown grambot:grambot /data
USER grambot
VOLUME ["/data"]

CMD ["python", "main.py"]
