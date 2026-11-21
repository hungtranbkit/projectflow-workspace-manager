# QA-only isolated Python test image; never built or executed by GitHub Actions.
# Build on a trusted QA host with a reviewed base image and dependencies.
# Runtime receives an exported source tree READ-ONLY and no signing material.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PIP_NO_CACHE_DIR=1
RUN python -m pip install --no-cache-dir \
    "pytest==9.2.0" "httpx>=0.27,<1" "fastapi>=0.110,<1" \
    "uvicorn>=0.27,<1" "jinja2>=3.1,<4" "python-multipart>=0.0.9,<1" \
    "PyYAML>=6,<7" "websockets>=12,<16" "ruamel.yaml>=0.18,<1" \
    "itsdangerous>=2.1,<3" "slowapi>=0.1.9,<1" "cryptography>=42,<51"
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python"]
