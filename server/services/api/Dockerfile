FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY packages/marquee_core /opt/marquee_core
RUN pip install --no-cache-dir -e /opt/marquee_core
# Alembic lives in the image so `make migrate` runs the SAME revisions the
# service was built with, rather than whatever happens to be on the host.
COPY services/api/alembic.ini /app/alembic.ini
COPY services/api/alembic /app/alembic
COPY services/api/app /app/app
# Tests ride along so `make be-test` can run them in this image.
COPY tests /app/tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
