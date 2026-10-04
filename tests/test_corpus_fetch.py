from __future__ import annotations

import hashlib
import http.client
import socket
import stat
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
from scripts import fetch_corpus_datasheet


class _FakeHTTPResponse(http.client.HTTPResponse):
    def __init__(self, payload: bytes, url: str = "https://www.ti.com/fixture.pdf") -> None:
        super().__init__(sock=socket.socket())
        self._payload = payload
        self._url = url

    def read(self, amt: int | None = None) -> bytes:
        return self._payload if amt is None else self._payload[:amt]

    def geturl(self) -> str:
        return self._url


def _manifest(path: Path, payload: bytes, *, url: str = "https://www.ti.com/fixture.pdf") -> None:
    path.write_text(
        (
            '{"schema":"circuit_golden_corpus","version":1,"entries":[{'
            '"id":"fixture","manufacturer":"Example","mpn":"FIXTURE",'
            '"package_family":"custom","datasheet":{"url":"'
            f'{url}","sha256":"{hashlib.sha256(payload).hexdigest()}",'
            '"revision":"A"},"truth_path":"truth/fixture.json",'
            '"truth_status":"unconfirmed","confirmed_by":null,"confirmed_at":null,'
            '"notes":"fixture"}]}'
        ),
        encoding="utf-8",
    )


def test_fetch_corpus_datasheet_downloads_only_manifest_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"%PDF-fixture"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)
    requests: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: int) -> _FakeHTTPResponse:
        requests.append(request)
        assert timeout == 60
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", fake_urlopen)
    cached = fetch_corpus_datasheet.fetch_corpus_datasheet(
        "fixture", tmp_path / "cache", manifest_path=manifest_path
    )

    assert cached.read_bytes() == payload
    assert stat.S_IMODE(cached.stat().st_mode) == 0o644
    assert len(requests) == 1
    assert requests[0].full_url == "https://www.ti.com/fixture.pdf"


def test_fetch_corpus_datasheet_refuses_hash_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, b"%PDF-expected")

    def wrong_response(*_args: object, **_kwargs: object) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(b"%PDF-wrong")

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", wrong_response)

    with pytest.raises(fetch_corpus_datasheet.CorpusFetchError, match="SHA-256 mismatch"):
        fetch_corpus_datasheet.fetch_corpus_datasheet(
            "fixture", tmp_path / "cache", manifest_path=manifest_path
        )
    assert not (tmp_path / "cache" / "fixture.pdf").exists()


def test_fetch_corpus_datasheet_rejects_aggregator_url(tmp_path: Path) -> None:
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, b"expected", url="https://example.com/fixture.pdf")

    with pytest.raises(
        fetch_corpus_datasheet.CorpusFetchError,
        match="approved manufacturer URL",
    ):
        fetch_corpus_datasheet.fetch_corpus_datasheet(
            "fixture", tmp_path / "cache", manifest_path=manifest_path
        )


def test_fetch_corpus_datasheet_rejects_redirect_to_aggregator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"%PDF-fixture"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)

    def redirected_response(*_args: object, **_kwargs: object) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(payload, "https://aggregator.example/fixture.pdf")

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", redirected_response)

    with pytest.raises(
        fetch_corpus_datasheet.CorpusFetchError,
        match="redirect target is not an approved manufacturer URL",
    ):
        fetch_corpus_datasheet.fetch_corpus_datasheet(
            "fixture", tmp_path / "cache", manifest_path=manifest_path
        )
    assert not (tmp_path / "cache" / "fixture.pdf").exists()


def test_fetch_corpus_datasheet_rejects_non_http_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"%PDF-fixture"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)

    def unexpected_response(_request: Request, *, timeout: int) -> BytesIO:
        assert timeout == 60
        return BytesIO(payload)

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", unexpected_response)

    with pytest.raises(
        fetch_corpus_datasheet.CorpusFetchError, match="unexpected datasheet response"
    ):
        fetch_corpus_datasheet.fetch_corpus_datasheet(
            "fixture", tmp_path / "cache", manifest_path=manifest_path
        )
    assert not (tmp_path / "cache" / "fixture.pdf").exists()


def test_fetch_corpus_datasheet_rejects_non_pdf_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"<html>not a PDF</html>"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)

    def html_response(_request: Request, *, timeout: int) -> _FakeHTTPResponse:
        assert timeout == 60
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", html_response)

    with pytest.raises(fetch_corpus_datasheet.CorpusFetchError, match="does not begin with %PDF-"):
        fetch_corpus_datasheet.fetch_corpus_datasheet(
            "fixture", tmp_path / "cache", manifest_path=manifest_path
        )
    assert not (tmp_path / "cache" / "fixture.pdf").exists()


def test_fetch_corpus_datasheet_redirect_handler_rejects_aggregator() -> None:
    handler = fetch_corpus_datasheet._ManufacturerRedirectHandler()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(
        fetch_corpus_datasheet.CorpusFetchError,
        match="redirect target is not an approved manufacturer URL",
    ):
        handler.redirect_request(
            Request("https://www.ti.com/fixture.pdf"),
            BytesIO(),
            302,
            "Found",
            Message(),
            "https://aggregator.example/fixture.pdf",
        )
