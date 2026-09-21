FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core curl && rm -rf /var/lib/apt/lists/*
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt yt-dlp==2024.12.13
COPY packages/marquee_core /opt/marquee_core
RUN pip install --no-cache-dir -e /opt/marquee_core
COPY services/player /app/player
COPY services/tftp /app/tftp
CMD ["python", "-m", "player.main"]
