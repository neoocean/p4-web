#!/bin/sh
# Container entrypoint: optionally establish SSL trust, then serve.
set -e

mkdir -p "${P4WEB_DATA:-/data}"

# Keep the p4 trust database on the data volume so fingerprints
# survive container recreation.
export P4TRUST="${P4TRUST:-${P4WEB_DATA:-/data}/p4trust}"

# For ssl: servers p4 refuses to talk until the fingerprint is trusted.
# PREFERRED: pin the exact fingerprint via P4WEB_P4FINGERPRINT so a MITM
# on first connect can't have its cert blindly accepted. Otherwise
# P4WEB_AUTO_TRUST=1 falls back to trust-on-first-use (fine only on a LAN
# you control; for anything else seed the p4trust volume yourself).
case "$P4WEB_P4PORT" in
  ssl:*)
    if [ -n "$P4WEB_P4FINGERPRINT" ]; then
      p4 -p "$P4WEB_P4PORT" trust -i "$P4WEB_P4FINGERPRINT"
    elif [ "${P4WEB_AUTO_TRUST:-0}" = "1" ]; then
      p4 -p "$P4WEB_P4PORT" trust -y || true
    fi
    ;;
esac

# --forwarded-allow-ips is pinned to the trusted proxy so a client can't
# spoof X-Forwarded-For. In Docker the proxy is usually not localhost —
# set P4WEB_FORWARDED_ALLOW_IPS to the reverse-proxy/bridge address.
exec uvicorn app.main:app --host "${P4WEB_HOST:-0.0.0.0}" --port "${P4WEB_PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips "${P4WEB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
