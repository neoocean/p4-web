FROM python:3.12-slim

ARG TARGETARCH
ARG P4_RELEASE=r25.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Perforce command-line client (all server access goes through it).
RUN case "$TARGETARCH" in \
      arm64) P4ARCH=linux26aarch64 ;; \
      *)     P4ARCH=linux26x86_64 ;; \
    esac \
    && curl -fsSL "https://ftp.perforce.com/perforce/${P4_RELEASE}/bin.${P4ARCH}/p4" -o /usr/local/bin/p4 \
    && chmod +x /usr/local/bin/p4 \
    && /usr/local/bin/p4 -V

WORKDIR /srv/p4web
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Sessions DB + p4 trust file live here; mount a volume to keep them.
ENV P4WEB_DATA=/data \
    P4WEB_HOST=0.0.0.0 \
    P4WEB_PORT=8080
VOLUME /data
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
