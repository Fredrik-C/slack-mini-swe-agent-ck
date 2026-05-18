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

RUN curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 9.0 --quality ga --install-dir /usr/share/dotnet --no-path \
    && /tmp/dotnet-install.sh --channel 8.0 --quality ga --install-dir /usr/share/dotnet --no-path \
    && rm -f /tmp/dotnet-install.sh

RUN npm install -g typescript

ARG PUID=1000
ARG PGID=1000
ARG CK_INSTALL_URL=https://raw.githubusercontent.com/Fredrik-C/ContextKing/main/scripts/install-global.sh

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY . /app

RUN curl -fsSL "${CK_INSTALL_URL}" | bash \
    && ln -sf /root/.ck/bin/ck /usr/local/bin/ck \
    && mkdir -p /home/appuser/.config/litellm \
    && chown -R "${PUID}:${PGID}" /app /home/appuser

ENV HOME=/home/appuser
USER ${PUID}:${PGID}

CMD ["python3", "app.py"]
