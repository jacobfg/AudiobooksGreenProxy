FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Установка зависимостей (используем кэш BuildKit для pip)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data
COPY audiobookshelf ./audiobookshelf
COPY tg_bot ./tg_bot
COPY utils ./utils

COPY *.py ./

ENTRYPOINT ["python", "books_proxy.py"]
