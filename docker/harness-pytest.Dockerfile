# Build on a trusted operator node. Local-only image for untrusted pytest output.
FROM python:3.12-slim
RUN python -m pip install --no-cache-dir pytest==9.0.2
USER 65534:65534
WORKDIR /repo
