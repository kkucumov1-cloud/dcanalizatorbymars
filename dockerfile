FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc libpq-dev libjpeg-dev zlib1g-dev libtiff5-dev libfreetype6-dev \
    liblcms2-dev libwebp-dev tcl-dev tk-dev libffi-dev wget curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV TELETHON_SESSION=dateregbot.session
CMD ["python", "dateregbot_full.py"]
