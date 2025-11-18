FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV NATS_URL=nats://nats:4222

EXPOSE 8004
CMD ["python", "main.py"]
