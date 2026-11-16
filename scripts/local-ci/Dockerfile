# Trusted first-party PRs only. Inner Docker daemon never receives the host socket.
FROM docker:27.5.1-dind
RUN apk add --no-cache bash git python3 py3-pip python3-dev build-base libffi-dev openssl-dev curl nodejs
RUN python3 -m venv /opt/ci-venv
ENV PATH="/opt/ci-venv/bin:${PATH}" VIRTUAL_ENV=/opt/ci-venv

# Integration tests intentionally use login shells; keep their interpreter consistent.
RUN printf '%s\n' 'export PATH="/opt/ci-venv/bin:$PATH"' > /etc/profile.d/ci-python.sh
