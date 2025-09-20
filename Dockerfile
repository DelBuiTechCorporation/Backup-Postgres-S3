# Dockerfile
FROM python:3.12-alpine

# instalar cliente postgres, cron e timezone data
RUN apk add --no-cache postgresql-client bash curl ca-certificates tzdata dcron

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py /app/backup.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV AWS_REGION=us-east-1

ENTRYPOINT ["/app/entrypoint.sh"]
