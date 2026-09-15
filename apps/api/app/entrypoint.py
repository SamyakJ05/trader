"""Start uvicorn trusting only the reverse proxy's forwarded headers.

The client IP is what the login and password-reset rate limiters key on, so
whoever uvicorn trusts to report it decides whether those limits mean
anything. `--forwarded-allow-ips='*'` trusts any peer, which lets an attacker
rotate a forged X-Forwarded-For and defeat the per-IP axis entirely.

The proxy's address on the compose network is assigned by Docker, so it is
resolved at start rather than hardcoded. Written in Python rather than shell
because the runtime image is python:slim, which carries neither getent nor
paste — and because a quoting mistake here fails open.
"""

import os
import socket
import sys

PROXY_HOST = os.environ.get("PROXY_HOST", "caddy")


def proxy_addresses() -> list[str]:
    """Every address the proxy hostname resolves to."""
    configured = os.environ.get("PROXY_IP", "").strip()
    if configured:
        return [part.strip() for part in configured.split(",") if part.strip()]
    try:
        infos = socket.getaddrinfo(PROXY_HOST, None)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def main() -> None:
    addresses = proxy_addresses()
    if not addresses:
        # Refuse rather than fall back to '*'. Widening silently would leave
        # the rate limits looking enforced while trusting a forgeable header.
        print(
            f"entrypoint: could not resolve '{PROXY_HOST}' and PROXY_IP is unset; "
            "refusing to start with an untrusted forwarded-for source.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    trusted = ",".join(addresses)
    print(f"entrypoint: trusting forwarded headers from {trusted}", flush=True)

    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            os.environ.get("UVICORN_WORKERS", "2"),
            "--proxy-headers",
            "--forwarded-allow-ips",
            trusted,
        ],
    )


if __name__ == "__main__":
    main()
