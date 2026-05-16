FROM mcr.microsoft.com/dotnet/sdk:10.0

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    bash \
    ca-certificates \
    curl \
    jq \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

ARG PUID=1000
ARG PGID=1000

RUN set -eux; \
    if ! getent group "${PGID}" >/dev/null; then \
        groupadd --gid "${PGID}" appgroup; \
    fi; \
    if ! getent group appgroup >/dev/null; then \
        groupadd appgroup; \
    fi; \
    if ! id -u appuser >/dev/null 2>&1; then \
        useradd --uid "${PUID}" --gid "${PGID}" --create-home --shell /bin/bash appuser; \
    fi

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY . /app

RUN chown -R appuser:appgroup /app

USER appuser

CMD ["python3", "app.py"]
