"""Entrypoint: run the wildlife MCP server over Streamable HTTP.

Usage:
    python -m hoseid.mcp_wildlife.app

Default: plain HTTP on WILDLIFE_MCP_HOST:WILDLIFE_MCP_PORT (0.0.0.0:8853),
the same posture as the hoselm corpus MCP on :8851 - ZeroTier/LAN is the
perimeter, and Claude Desktop reaches it through the mcp-remote stdio bridge
in claude_desktop_config.json (its Settings-page connector form insists on
https AND probes from Anthropic's cloud, which cannot reach a ZeroTier
address, so that form is not usable for LAN servers).

WILDLIFE_MCP_TLS=1 instead serves https on the main port with a self-signed
cert (auto-generated under data/ssl/, SAN incl. LAN + ZeroTier IPs) plus a
loopback plain-HTTP listener on 127.0.0.1:WILDLIFE_MCP_HTTP_PORT (8854; 0
disables) for local clients that do not trust the cert.

Environment:
    WILDLIFE_MCP_HOST / WILDLIFE_MCP_PORT / WILDLIFE_MCP_HTTP_PORT / WILDLIFE_MCP_TLS
    WILDLIFE_MCP_CERTFILE / WILDLIFE_MCP_KEYFILE   override the cert location
    WILDLIFE_MCP_SAN_EXTRA   extra SAN entries (comma list) when generating
    HOSEID_REVIEW_URL        review-app base for review_url (default http://localhost:8870)
    WILDLIFE_LAT / WILDLIFE_LON   property coords for sunrise (default from stations.json)
    HOSEID_ROOT              default ~/trailcam
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from hoseid.mcp_wildlife.server import DEFAULT_HOST, DEFAULT_PORT, build_starlette_app

log = logging.getLogger(__name__)


def _truthy(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def build_servers() -> list[uvicorn.Server]:
    host = os.environ.get("WILDLIFE_MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("WILDLIFE_MCP_PORT", str(DEFAULT_PORT)))
    http_port = int(os.environ.get("WILDLIFE_MCP_HTTP_PORT", "8854"))
    use_tls = _truthy(os.environ.get("WILDLIFE_MCP_TLS"), False)

    # One app per listener: FastMCP's StreamableHTTP session manager runs its
    # lifespan once per instance, so two uvicorn servers cannot share an app.
    servers: list[uvicorn.Server] = []
    if use_tls:
        from hoseid.mcp_wildlife.tls import ensure_selfsigned_cert
        cert, key = ensure_selfsigned_cert()
        servers.append(uvicorn.Server(uvicorn.Config(
            build_starlette_app(host=host, port=port), host=host, port=port, log_level="info",
            ssl_certfile=str(cert), ssl_keyfile=str(key))))
        log.info("wildlife MCP: https://%s:%s/mcp (cert %s)", host, port, cert)
        if http_port:
            servers.append(uvicorn.Server(uvicorn.Config(
                build_starlette_app(host="127.0.0.1", port=http_port), host="127.0.0.1",
                port=http_port, log_level="info")))
            log.info("wildlife MCP: http://127.0.0.1:%s/mcp (loopback only)", http_port)
    else:
        servers.append(uvicorn.Server(uvicorn.Config(
            build_starlette_app(host=host, port=port), host=host, port=port, log_level="info")))
        log.info("wildlife MCP: http://%s:%s/mcp", host, port)
    return servers


async def _serve_all(servers: list[uvicorn.Server]) -> None:
    # Only one server may own signal handlers; the others just serve.
    for s in servers[1:]:
        s.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WILDLIFE_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    servers = build_servers()
    try:
        asyncio.run(_serve_all(servers))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
