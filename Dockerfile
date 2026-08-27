###############################################################################
# Terraform Service Generator — Dockerfile
# Multi-stage: installs Terraform in stage 1, copies only what's needed
###############################################################################

# ── Stage 1: download Terraform binary ────────────────────────────────────
FROM python:3.12-slim AS terraform-installer

ARG TERRAFORM_VERSION=1.8.5
ARG TARGETARCH=amd64

RUN apt-get update && apt-get install -y --no-install-recommends wget unzip ca-certificates \
    && wget -q \
       "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
       -O /tmp/terraform.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/terraform \
    && rm /tmp/terraform.zip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Stage 2: application image ────────────────────────────────────────────
FROM python:3.12-slim

# Copy Terraform from stage 1
COPY --from=terraform-installer /usr/local/bin/terraform /usr/local/bin/terraform

# System deps (ca-certs needed for HTTPS to registry.terraform.io)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY generator/ generator/
COPY static/   static/
COPY main.py   .

# Create outputs dir and set ownership
RUN mkdir -p outputs && chown -R appuser:appuser /app

# Terraform plugin cache (avoids re-downloading providers on every request)
ENV TF_PLUGIN_CACHE_DIR=/app/.terraform.d/plugin-cache
RUN mkdir -p /app/.terraform.d/plugin-cache \
    && chown -R appuser:appuser /app/.terraform.d

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
