from types import SimpleNamespace

import pytest

from armature.server.app.deps import Server
from armature.server.config import ServerConfig


def _insecure(origin: str, allow: bool) -> bool:
    cfg = ServerConfig(public_origin=origin, allow_private_http=allow)
    return Server.insecure_localhost.fget(SimpleNamespace(cfg=cfg))


@pytest.mark.parametrize("origin", ["http://192.168.1.251:8098", "http://10.0.0.5", "http://172.16.4.2:80"])
def test_private_http_origin_only_when_allowed(origin):
    assert _insecure(origin, allow=True)
    assert not _insecure(origin, allow=False)


@pytest.mark.parametrize("origin", ["http://8.8.8.8:8098", "http://example.com", "https://192.168.1.251",
                                    "http://192.168.1.251.evil.com", "http://999.1.1.1"])
def test_public_or_named_origins_stay_secure(origin):
    assert not _insecure(origin, allow=True)


def test_localhost_needs_no_flag():
    assert _insecure("http://localhost:8098", allow=False)
