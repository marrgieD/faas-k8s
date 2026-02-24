FROM python:3.9-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx kubernetes

COPY gateway /app/gateway

ENV FAAS_IN_CLUSTER=true \
    FAAS_NAMESPACE=default \
    FAAS_SERVICE_PORT=8080

EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]