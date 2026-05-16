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
    openssh-client \
    bash \
    ca-certificates \
    curl \
    jq \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

ARG PUID=1000
ARG PGID=1000

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY . /app

RUN mkdir -p /home/appuser/.config/litellm \
    && chown -R "${PUID}:${PGID}" /app /home/appuser

ENV HOME=/home/appuser
USER ${PUID}:${PGID}

CMD ["python3", "app.py"]
