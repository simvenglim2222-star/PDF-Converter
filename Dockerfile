FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicitly copy only the bot script (and any other needed files)
COPY Bot.py .

CMD ["python", "Bot.py"]
