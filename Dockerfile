FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Durable storage lives on a volume, not the container's writable layer, so
# data survives `docker restart` / container recreation, not just process
# kill -9. See DESIGN.md.
ENV WALLET_DB_PATH=/data/wallet.db
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
