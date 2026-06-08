"""Canonical SSRF-safe URL validation shared by the install fetcher and proxy.

Both the install endpoint (fetches arbitrary manifest/entry/icon URLs on the
owner's behalf) and the mini-app proxy (`routes/proxy.py`, CORS-bypass fetch)
reach external URLs from inside the network-privileged container. That makes
each an SSRF surface: a hostile URL could probe loopback (our own API), the
Docker bridge (sibling containers), or cloud metadata
(169.254.169.254 → IAM credentials). They MUST share one validator — the two
once drifted, and the proxy's `ip.is_global`-only check missed NAT64
(`64:ff9b::a9fe:a9fe`.is_global is True) and `::127.0.0.1`, a live bypass the
install path already closed. Keep the logic here, used by both.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

# Networks the fetcher must never reach. Hitting them from
# our (network-privileged) backend turns the install endpoint into
# an SSRF springboard: a malicious manifest URL could probe the
# container's own loopback (own API, metrics), Docker bridge
# (sibling services), cloud-provider metadata (169.254.169.254 →
# IAM credentials on AWS / GCP / Azure), or any other internal
# resource the container can reach.
_BLOCKED_NETS = [
  ipaddress.ip_network("0.0.0.0/8"),
  ipaddress.ip_network("10.0.0.0/8"),
  ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
  ipaddress.ip_network("127.0.0.0/8"),
  ipaddress.ip_network("169.254.0.0/16"),    # link-local + cloud metadata
  ipaddress.ip_network("172.16.0.0/12"),
  ipaddress.ip_network("192.168.0.0/16"),
  ipaddress.ip_network("::1/128"),
  ipaddress.ip_network("fc00::/7"),          # ULA
  ipaddress.ip_network("fe80::/10"),         # link-local IPv6
  # NAT64 well-known prefix — a resolver can hand back 64:ff9b::<v4> for a
  # blocked IPv4 (e.g. 64:ff9b::a9fe:a9fe == 169.254.169.254), which the
  # ipv4_mapped check below does NOT catch (that only handles ::ffff:). The
  # install fetcher has no legitimate need to reach a host only via NAT64, so
  # block the whole prefix.
  ipaddress.ip_network("64:ff9b::/96"),
]


def validate_url_safe(url: str) -> tuple[str, str, str]:
  """Validates a URL against the SSRF blocklist; returns `(pinned_url, host_header, sni_host)`.

  `pinned_url` connects to the resolved IP (defeats DNS-rebinding); `host_header`
  is the original authority host[:port] for the Host header; `sni_host` is the
  bare DNS name for TLS SNI/cert validation (see the return site below).

  The install endpoint is the SSRF surface: we fetch arbitrary URLs on behalf
  of an authenticated owner. From inside the container we can reach our own
  loopback (the Möbius API itself), the Docker bridge (sibling containers),
  and cloud metadata services (IAM credential exfiltration on AWS/GCP/Azure).
  We reject those targets, then PIN the fetch to the exact IP we validated:
  `pinned_url` has the validated IP substituted into the netloc, and the caller
  connects to it with `host` as the TLS SNI + Host header (see `_http_get`).

  Pinning closes the DNS-rebinding / TOCTOU gap: httpx would otherwise
  re-resolve the hostname at connect time, so a TTL-0 flip could aim the
  connection at an internal IP this validation never saw. Connecting to the
  already-validated IP makes the checked address and the fetched address the
  same one.
  """
  parsed = urlparse(url)
  if parsed.scheme not in ("http", "https"):
    raise HTTPException(
      400, f"URL scheme must be http or https, got {parsed.scheme!r}",
    )
  # Reject embedded credentials: manifest URLs are public, and userinfo would
  # be silently dropped when we rebuild the netloc around the pinned IP (httpx
  # otherwise turns it into a Basic-auth header), so a credentialed URL is both
  # a red flag and a footgun. Block it outright.
  if parsed.username or parsed.password:
    raise HTTPException(
      400, "URL must not contain credentials (user:pass@) — manifests are public.",
    )
  host = parsed.hostname
  if not host:
    raise HTTPException(400, f"URL is missing a hostname: {url}")
  try:
    infos = socket.getaddrinfo(host, None)
  except socket.gaierror as exc:
    raise HTTPException(400, f"Cannot resolve host {host!r}: {exc}")
  pinned_ip = None
  for info in infos:
    ip_str = info[4][0]
    try:
      ip = ipaddress.ip_address(ip_str)
    except ValueError:
      continue
    # An IPv6 that EMBEDS an IPv4 reaches that v4 host but won't match the
    # IPv4 entries in _BLOCKED_NETS: ::ffff:a.b.c.d (mapped, e.g.
    # ::ffff:169.254.169.254) and ::a.b.c.d (IPv4-compatible / ::/96, e.g. a
    # literal [::127.0.0.1] URL). Pull the embedded v4 and check it too.
    # (Well-known NAT64 64:ff9b::/96 is blocked as a whole prefix above.)
    candidates = [ip]
    if ip.version == 6:
      if ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)
      elif ip in ipaddress.ip_network("::/96"):
        candidates.append(ipaddress.ip_address(int(ip) & 0xFFFFFFFF))
    for cand in candidates:
      for net in _BLOCKED_NETS:
        if cand in net:
          raise HTTPException(
            400,
            f"URL {host!r} resolves to blocked address {ip} "
            f"(network {net}).",
          )
    # Every resolved address is validated (we raise on the first blocked one),
    # so pinning to the first is safe — the fetched IP can't be an unvalidated
    # one.
    if pinned_ip is None:
      pinned_ip = ip_str
  if pinned_ip is None:
    raise HTTPException(400, f"Cannot resolve host {host!r} to any address.")
  ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
  netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
  pinned_url = parsed._replace(netloc=netloc).geturl()
  # Host header carries the ORIGINAL authority (host + non-default port, IPv6
  # brackets preserved) per RFC 7230 §5.4; the SNI/cert name is the bare DNS
  # host. userinfo was rejected above, so parsed.netloc is exactly host[:port].
  return pinned_url, parsed.netloc, host
