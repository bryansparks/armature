"""Vendor API calls for connections: Verify for each kind, and GitHub App installation tokens.

Every call takes an injectable httpx transport, so tests never reach a vendor.
Every call goes only to https on a host that resolves to public addresses, and
never follows a redirect. An error names the call and its HTTP status, never
the response body.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

TIMEOUT = 10.0
POLL_TIMEOUT = 30.0
POLL_BODY_MAX = 1024 * 1024


class VendorError(Exception):
    """A vendor call failed. The message is safe to show and store."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Verified:
    identity: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    installations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Item:
    """One issue a poll found. ``fields`` is what a trigger's inputs map from."""
    key: str
    title: str | None
    url: str | None
    fields: dict[str, Any]


def addresses(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


# Tests replace this, so no test resolves a real name.
resolve_host = addresses


NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _effective(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The address a packet really reaches: an IPv4 address wrapped in IPv6 is judged as that IPv4 address."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.sixtofour:
            return ip.sixtofour
    return ip


def public_address(url: str, what: str) -> str:
    """The one address to connect to for ``url``, after refusing anything but https to public addresses."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise VendorError(f"{what} must use https.")
    try:
        found = [str(ipaddress.ip_address(parts.hostname))]
    except ValueError:
        try:
            found = resolve_host(parts.hostname)
        except (OSError, UnicodeError) as exc:
            raise VendorError(f"{what} has a host that doesn't resolve.") from exc
    try:
        public = bool(found) and all(_effective(a).is_global for a in found)
    except ValueError:
        public = False
    if not public:
        raise VendorError(f"{what} has a host that resolves to a private address.")
    return found[0].split("%", 1)[0]


def client(transport: httpx.BaseTransport | None, timeout: float = TIMEOUT) -> httpx.Client:
    # A proxy from the environment would tunnel to the pinned IP and drop the TLS server name.
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)


def call(http: httpx.Client, method: str, url: str, what: str, *, headers: dict[str, str] | None = None,
         auth: Any = None, stream: bool = False, **kwargs: Any) -> httpx.Response:
    """One request to the address that was checked, so a second DNS answer can't redirect it.

    The Host header and TLS server name stay the vendor's, so the certificate is still verified against its name.
    """
    target = httpx.URL(url)
    address = public_address(url, what)
    request = http.build_request(method, target.copy_with(host=address),
                                 headers={**(headers or {}), "Host": target.netloc.decode("ascii")},
                                 extensions={"sni_hostname": target.host}, **kwargs)
    try:
        return http.send(request, auth=auth, stream=stream)
    except httpx.HTTPError as exc:
        raise VendorError(f"{what} could not be reached.") from exc


def json_of(response: httpx.Response, what: str, limit: int | None = None) -> Any:
    if response.status_code >= 300:
        raise VendorError(f"{what} answered HTTP {response.status_code}.", response.status_code)
    if limit is not None:
        return _bounded(response, what, limit)
    try:
        return response.json()
    except ValueError as exc:
        raise VendorError(f"{what} did not answer with JSON.") from exc


def _bounded(response: httpx.Response, what: str, limit: int) -> Any:
    """The JSON of a streamed response, read no further than ``limit`` bytes."""
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > limit:
                raise VendorError(f"{what} answered with more than {limit // 1024} KB.")
    except httpx.HTTPError as exc:
        raise VendorError(f"{what} could not be read.") from exc
    finally:
        response.close()
    try:
        return json.loads(body)
    except ValueError as exc:
        raise VendorError(f"{what} did not answer with JSON.") from exc
