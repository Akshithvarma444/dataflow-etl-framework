FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Installed before the source is copied so code changes reuse this layer.
# The optional extra_ca secret is for machines whose antivirus or proxy re-signs
# HTTPS traffic (see README, Troubleshooting). It adds a trusted CA for this step
# only and is not stored in the image; TLS verification always stays on.
COPY requirements.txt .
RUN --mount=type=secret,id=extra_ca,required=false sh -c '\
    if [ -s /run/secrets/extra_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; \
        export PIP_CERT=/tmp/ca.pem; \
    fi; \
    pip install --no-cache-dir -r requirements.txt; \
    status=$?; rm -f /tmp/ca.pem; exit $status'

COPY pyproject.toml .
COPY config/ config/
COPY src/ src/
COPY tests/ tests/

ENTRYPOINT ["python", "-m", "src.main"]
