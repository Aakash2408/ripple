FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# Use shell form so $PORT gets expanded at runtime
CMD uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8000}
