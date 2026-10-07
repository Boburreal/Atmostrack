FROM python:3.10-slim

# FFmpeg o'rnatish
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Kutubxonalarni o'rnatish
RUN pip install --no-cache-dir pyTelegramBotAPI fastapi uvicron requests python-multipart

COPY . .

CMD ["python", "bot.py"]
