FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi pytest uvicorn

RUN mkdir -p /skill/cache

COPY backend/web_runtime_host.py /runtime/web_runtime_host.py
COPY backend/web_runtime_capabilities.py /runtime/web_runtime_capabilities.py
COPY backend/web_runtime_relay.py /runtime/web_runtime_relay.py

WORKDIR /skill

ENV PYTHONPATH=/skill:/skill/.deps:/runtime
