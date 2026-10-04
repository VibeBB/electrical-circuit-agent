from __future__ import annotations

import hashlib
import stat
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
from scripts import fetch_corpus_datasheet


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
    payload = b"fixture PDF"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)
    requests: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request)
        assert timeout == 60
        return BytesIO(payload)

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
    _manifest(manifest_path, b"expected")

    def wrong_response(*_args: object, **_kwargs: object) -> BytesIO:
        return BytesIO(b"wrong")

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
    payload = b"fixture PDF"
    manifest_path = tmp_path / "corpus.json"
    _manifest(manifest_path, payload)

    class RedirectedResponse(BytesIO):
        def geturl(self) -> str:
            return "https://aggregator.example/fixture.pdf"

    def redirected_response(*_args: object, **_kwargs: object) -> RedirectedResponse:
        return RedirectedResponse(payload)

    monkeypatch.setattr(fetch_corpus_datasheet, "_open_url", redirected_response)

    with pytest.raises(
        fetch_corpus_datasheet.CorpusFetchError,
        match="redirect target is not an approved manufacturer URL",
    ):
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
            None,
            302,
            "Found",
            {},
            "https://aggregator.example/fixture.pdf",
        )
