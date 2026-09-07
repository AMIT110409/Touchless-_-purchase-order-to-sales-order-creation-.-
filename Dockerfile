# ============================================================
# Dockerfile — Touchless PO-to-SO Pipeline
# ============================================================
# Two targets:
#   1. "cpu" (default)  → Claude API only, no GPU needed
#                          Use for: Azure Container Jobs (daily scheduler)
#   2. "gpu"            → Adds CUDA + Torch for local Qwen/LLaMA inference
#                          Use for: GPU-enabled Azure Container Apps
#
# Build (CPU):  docker build --target cpu -t po-pipeline:latest .
# Build (GPU):  docker build --target gpu -t po-pipeline:gpu .
# ============================================================

# ─────────────────────────────────────────────────────────────
# BASE STAGE — shared system packages
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libgomp1 \
    poppler-utils \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ─────────────────────────────────────────────────────────────
# DEPENDENCIES STAGE — install Python packages
# ─────────────────────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────
# CPU TARGET — Claude API mode (no GPU, small image)
# ─────────────────────────────────────────────────────────────
FROM deps AS cpu

# Copy all source code (new structure)
COPY src/              ./src/
COPY config/           ./config/
COPY .env.example      .env.example

# Create output directories
RUN mkdir -p reports .celonis_cache

# Default: run Stage 1 pipeline
# Override with: docker run ... python src/pipeline/run_celonis_feedback.py --current-run
CMD ["python", "src/pipeline/run_outlook_to_pipeline.py"]

# ─────────────────────────────────────────────────────────────
# GPU TARGET — adds CUDA + PyTorch for local LLM inference
# ─────────────────────────────────────────────────────────────
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04 AS gpu

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3-pip python3.11-dev \
    build-essential libgl1-mesa-glx libglib2.0-0 libgomp1 \
    poppler-utils git \
    && ln -s /usr/bin/python3.11 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-gpu.txt ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-gpu.txt

COPY src/      ./src/
COPY config/   ./config/
RUN mkdir -p reports .celonis_cache

CMD ["python", "src/pipeline/run_outlook_to_pipeline.py"]
