"""Self-signed TLS for the wildlife MCP.

Claude Desktop's connector form insists on an https:// URL but connects
directly from the user's machine over ZeroTier, so a self-signed cert that
the client Mac trusts is enough. The SAN covers the hostname, .local, every
local interface IP (LAN + ZeroTier) and 127.0.0.1, so the same cert works
whichever address the connector uses. Same recipe as hoseserv/app/ssl.py.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "ssl"


def cert_paths() -> tuple[Path, Path]:
    cert = Path(os.environ.get("WILDLIFE_MCP_CERTFILE") or DEFAULT_DIR / "selfsigned.crt")
    key = Path(os.environ.get("WILDLIFE_MCP_KEYFILE") or DEFAULT_DIR / "selfsigned.key")
    return cert.expanduser(), key.expanduser()


def local_ips() -> list[str]:
    """IPv4 addresses of every interface (macOS/BSD ifconfig), loopback excluded."""
    ips: list[str] = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        ips = re.findall(r"^\s*inet (\d+\.\d+\.\d+\.\d+)", out, flags=re.M)
    except (OSError, subprocess.SubprocessError):
        pass
    if not ips:
        try:
            ips = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except socket.gaierror:
            pass
    seen: list[str] = []
    for ip in ips:
        if not ip.startswith("127.") and ip not in seen:
            seen.append(ip)
    return seen


def san_entries(extra: str | None = None) -> str:
    host = socket.gethostname().split(".")[0]
    entries = ["DNS:localhost", f"DNS:{host}", f"DNS:{host}.local", "IP:127.0.0.1"]
    entries += [f"IP:{ip}" for ip in local_ips()]
    for e in (extra or os.environ.get("WILDLIFE_MCP_SAN_EXTRA", "")).split(","):
        e = e.strip()
        if e and e not in entries:
            entries.append(e if ":" in e else (f"IP:{e}" if re.match(r"^\d+\.\d+\.\d+\.\d+$", e) else f"DNS:{e}"))
    return ",".join(entries)


def ensure_selfsigned_cert() -> tuple[Path, Path]:
    cert, key = cert_paths()
    if cert.exists() and key.exists():
        return cert, key
    cert.parent.mkdir(parents=True, exist_ok=True)
    san = san_entries()
    log.info("generating self-signed cert %s (SAN %s)", cert, san)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
         "-days", "825", "-nodes", "-subj", f"/CN={socket.gethostname().split('.')[0]}",
         "-addext", f"subjectAltName={san}",
         # Apple's TLS trust rules (macOS 10.15+/iOS 13+): SAN present, <=825 days,
         # RSA>=2048, and an EKU containing serverAuth - without the EKU the cert
         # is rejected even when the user has marked it trusted in Keychain.
         "-addext", "extendedKeyUsage=serverAuth",
         "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
         "-addext", "basicConstraints=critical,CA:TRUE"],
        check=True, capture_output=True)
    os.chmod(key, 0o600)
    return cert, key
