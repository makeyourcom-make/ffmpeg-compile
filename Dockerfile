FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Use gunicorn with generous timeout — background threads handle long jobs,
# but the worker must stay alive while threads run.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "1800", "--workers", "1", "--threads", "4", "app:app"]
