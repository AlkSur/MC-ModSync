"""trust 模块：内置可信根 + 证书校验失败自动重试。"""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from mcmodsync import trust


@pytest.fixture(autouse=True)
def _reset_trust_state(monkeypatch):
    """每个用例前重置粘性状态，避免用例间串扰。"""
    monkeypatch.setattr(trust, "_pinned_ok", False)
    monkeypatch.setattr(trust, "_pinned_tried", False)


def _verify_err(url="https://example.com/x"):
    reason = urllib.error.URLError(
        "certificate verify failed: certificate has expired (_ssl.c:1010)")
    # 与真实 urlopen 一致：URLError 包 SSLError；SSLCertVerificationError 是其子类
    import ssl
    err = urllib.error.URLError(ssl.SSLCertVerificationError(
        20, "certificate verify failed: certificate has expired"))
    err.filename = url
    return err


class _Resp:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_pinned_context_has_roots() -> None:
    ctx = trust.pinned_context()
    certs = ctx.get_ca_certs()
    assert len(certs) == 8                      # ISRG×2 + GTS×3 + GlobalSign + Amazon + Starfield
    assert ctx.check_hostname
    subjects = set()
    for c in certs:
        for rdn in c.get("subject", ()):
            for key, val in rdn:
                if key == "commonName":
                    subjects.add(val)
    assert "ISRG Root X1" in subjects
    assert "Amazon Root CA 1" in subjects


def test_open_https_falls_back_to_pinned(monkeypatch) -> None:
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(context is not None)       # True = 用了内置根上下文
        if len(calls) == 1:                     # 第一次（默认库）失败
            raise _verify_err()
        return _Resp("pinned")

    monkeypatch.setattr(trust.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("https://example.com/x")
    with trust.open_https(req, timeout=5) as r:
        assert r.tag == "pinned"
    assert calls == [False, True]
    assert trust.pinned_mode()
    assert trust.pinned_tried()


def test_open_https_sticky(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None, context=None):
        assert context is not None              # 粘性模式应直接走内置根
        return _Resp("pinned")

    monkeypatch.setattr(trust, "_pinned_ok", True)
    monkeypatch.setattr(trust.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("https://example.com/x")
    with trust.open_https(req, timeout=5) as r:
        assert r.tag == "pinned"


def test_open_https_no_retry_on_plain_network_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(trust.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("https://example.com/x")
    with pytest.raises(urllib.error.URLError):
        trust.open_https(req, timeout=5)
    assert not trust.pinned_tried()             # 非证书类错误不触发内置根


def test_open_https_reraises_original_when_pinned_also_fails(monkeypatch) -> None:
    original = _verify_err()

    def fake_urlopen(req, timeout=None, context=None):
        if context is None:
            raise original
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(trust.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("https://example.com/x")
    with pytest.raises(urllib.error.URLError) as ei:
        trust.open_https(req, timeout=5)
    assert ei.value is original                 # 抛原始证书错误（诊断基于它）
    assert trust.pinned_tried()
    assert not trust.pinned_mode()              # 内置库没成功，不进粘性


def test_is_cert_verify_error_variants() -> None:
    assert trust.is_cert_verify_error(_verify_err())
    assert not trust.is_cert_verify_error(urllib.error.URLError("timed out"))
    assert not trust.is_cert_verify_error(ValueError("x"))


def test_http_download_open_fallback(monkeypatch) -> None:
    """下载层 _open：证书类失败应换内置根 opener 重试。"""
    from mcmodsync import http_download as hd

    opened = []

    class _Opener:
        def __init__(self, kind):
            self.kind = kind

        def open(self, req, timeout=None):
            opened.append(self.kind)
            if self.kind == "default":
                raise _verify_err()
            return _Resp("dl")

    monkeypatch.setattr(hd.urllib.request, "build_opener",
                        lambda *h: _Opener("pinned" if h else "default"))
    monkeypatch.setattr(hd.urllib.error, "URLError", urllib.error.URLError)
    req = urllib.request.Request("https://example.com/f.jar")
    with hd._open(_Opener("default"), req, 10) as r:
        assert r.tag == "dl"
    assert opened == ["default", "pinned"]
    assert trust.pinned_mode()
