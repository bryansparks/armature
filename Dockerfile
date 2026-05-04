# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

RUN pip install --no-cache-dir uv==0.4.*

WORKDIR /app

COPY pyproject.toml .
COPY armature/__init__.py armature/__init__.py

RUN uv pip install --system ".[service]"

COPY armature/ armature/

EXPOSE 8080

CMD ["armature", "serve", "--host", "0.0.0.0", "--port", "8080"]
