FROM python:3.12-slim

ARG TARGETARCH
ARG P4_RELEASE=r25.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Perforce command-line client (all server access goes through it).
# The download is pinned to a per-arch SHA-256 from Perforce's published
# SHA256SUMS for ${P4_RELEASE}; a corrupted mirror or a TLS MITM that
# swaps the binary is rejected before it is ever made executable. Bump
# both hashes when P4_RELEASE changes.
RUN case "$TARGETARCH" in \
      arm64) P4ARCH=linux26aarch64; P4SHA=37b691225d442b6fbbee6be069ee6c087766774769593479f041e96ec0871515 ;; \
      *)     P4ARCH=linux26x86_64;  P4SHA=0fda09fd6c572e0c267e1c7108383ae4293b40fddee8ebb01e834883c45e481d ;; \
    esac \
    && curl -fsSL "https://ftp.perforce.com/perforce/${P4_RELEASE}/bin.${P4ARCH}/p4" -o /usr/local/bin/p4 \
    && echo "${P4SHA}  /usr/local/bin/p4" | sha256sum -c - \
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

# Run as an unprivileged user, not root: any RCE/container-escape then
# starts without UID 0. /data is created and owned here so a fresh named
# volume inherits appuser ownership on first mount.
RUN useradd --system --uid 10001 --home-dir /srv/p4web appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /srv/p4web
USER appuser

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
