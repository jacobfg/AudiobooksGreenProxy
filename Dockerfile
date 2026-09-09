# Step 1: Fetch the static FFmpeg binaries
FROM mwader/static-ffmpeg:6.0-1 AS ffmpeg

# Step 2: Build the actual Python runtime environment
FROM python:3.12-slim

# Copy only the compiled FFmpeg and FFprobe binaries from the first stage
COPY --from=ffmpeg /ffmpeg /usr/local/bin/
COPY --from=ffmpeg /ffprobe /usr/local/bin/

# Set up your application directory
WORKDIR /app

# (Optional) Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data
COPY audiobookshelf ./audiobookshelf
COPY tg_bot ./tg_bot
COPY utils ./utils

COPY *.py ./

ENTRYPOINT ["python", "books_proxy.py"]
