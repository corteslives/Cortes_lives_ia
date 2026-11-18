FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PORT=8000 DATA_DIR=/app/data WHISPER_MODEL=tiny GEMINI_MODEL=gemini-2.5-flash
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "3600", "app:app"]
