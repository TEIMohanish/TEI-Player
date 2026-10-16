FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/app.py backend/cleanup.py /app/
EXPOSE 5055
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5055", "--timeout", "120", "app:app"]
