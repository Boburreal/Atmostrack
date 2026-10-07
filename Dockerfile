FROM python:3.10-slim

# Установка FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Установка библиотек (исправлено uvicorn)
RUN pip install --no-cache-dir pyTelegramBotAPI fastapi uvicorn requests python-multipart

COPY . .

CMD ["python", "bot.py"]
