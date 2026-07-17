#!/bin/sh
# Container entrypoint: optionally establish SSL trust, then serve.
set -e

mkdir -p "${P4WEB_DATA:-/data}"

# Keep the p4 trust database on the data volume so fingerprints
# survive container recreation.
export P4TRUST="${P4TRUST:-${P4WEB_DATA:-/data}/p4trust}"

# For ssl: servers p4 refuses to talk until the fingerprint is
# trusted. P4WEB_AUTO_TRUST=1 accepts it on first connect
# (trust-on-first-use — fine on a LAN you control; for anything
# else run `p4 trust` against the volume yourself).
if [ "${P4WEB_AUTO_TRUST:-0}" = "1" ] && [ -n "$P4WEB_P4PORT" ]; then
  case "$P4WEB_P4PORT" in
    ssl:*) p4 -p "$P4WEB_P4PORT" trust -y || true ;;
  esac
fi

# --forwarded-allow-ips is pinned to the trusted proxy so a client can't
# spoof X-Forwarded-For. In Docker the proxy is usually not localhost —
# set P4WEB_FORWARDED_ALLOW_IPS to the reverse-proxy/bridge address.
exec uvicorn app.main:app --host "${P4WEB_HOST:-0.0.0.0}" --port "${P4WEB_PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips "${P4WEB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
