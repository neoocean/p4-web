#!/bin/sh
# Start p4-web. Configure the Perforce server with P4WEB_P4PORT (falls
# back to P4PORT from the environment).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
# --proxy-headers: trust X-Forwarded-* from local proxies (tailscale
# serve, reverse proxies on this host) so rate limiting sees real IPs.
# --forwarded-allow-ips is pinned to the trusted proxy (localhost by
# default) so a directly-connecting client can't spoof its IP; override
# with P4WEB_FORWARDED_ALLOW_IPS when the proxy is elsewhere.
exec .venv/bin/uvicorn app.main:app --host "${P4WEB_HOST:-127.0.0.1}" --port "${P4WEB_PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips "${P4WEB_FORWARDED_ALLOW_IPS:-127.0.0.1}" "$@"
