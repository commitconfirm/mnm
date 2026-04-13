FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    mkdir -p /root/.cache && \
    python3 -c "from mac_vendor_lookup import MacLookup; MacLookup().update_vendors()" 2>/dev/null || true

COPY app/ ./app/

EXPOSE 9090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090"]
