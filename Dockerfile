FROM python:3.11-slim

WORKDIR /app

# Install system dependencies required for Pillow and network sockets
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY frame_art_daemon.py .
COPY frame_studio.html .
COPY nature_canvas.html .
COPY config.example.json .

# Volume for persisted artwork, caches, and configuration
VOLUME ["/app/data"]

EXPOSE 8098

CMD ["python", "frame_art_daemon.py"]
