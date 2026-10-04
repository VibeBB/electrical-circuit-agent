from __future__ import annotations

import argparse
import email.message
import hashlib
import http.client
import os
import tempfile
from pathlib import Path
from typing import IO, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from circuit import corpus

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _REPO_ROOT / "library" / "corpus" / "corpus.json"
_OFFICIAL_HOSTS = {"ti.com", "www.ti.com", "ww1.microchip.com"}


class CorpusFetchError(ValueError):
    pass


def _is_approved_manufacturer_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in _OFFICIAL_HOSTS


class _ManufacturerRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: email.message.Message,
        newurl: str,
    ) -> Request | None:
        if not _is_approved_manufacturer_url(newurl):
            raise CorpusFetchError("datasheet redirect target is not an approved manufacturer URL")
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            cast(http.client.HTTPMessage, headers),
            newurl,
        )


def _open_url(request: Request, *, timeout: int) -> object:
    opener = build_opener(_ManufacturerRedirectHandler())
    return opener.open(request, timeout=timeout)


def fetch_corpus_datasheet(
    entry_id: str,
    cache_dir: Path,
    *,
    manifest_path: Path = _MANIFEST_PATH,
) -> Path:
    manifest = corpus.load_manifest(manifest_path)
    entry = next((item for item in manifest.entries if item.id == entry_id), None)
    if entry is None:
        raise CorpusFetchError(f"unknown corpus entry: {entry_id}")

    if not _is_approved_manufacturer_url(entry.datasheet.url):
        raise CorpusFetchError("datasheet URL is not an approved manufacturer URL")

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{entry.id}.pdf"
    if target.is_file() and corpus.sha256(target) == entry.datasheet.sha256:
        target.chmod(0o644)
        return target

    request = Request(entry.datasheet.url, headers={"User-Agent": "circuit-corpus-fetch/1"})
    try:
        opened_response = _open_url(request, timeout=60)
        if not isinstance(opened_response, http.client.HTTPResponse):
            raise CorpusFetchError(
                f"unexpected datasheet response type: {type(opened_response).__name__}"
            )
        with opened_response as response:
            final_url = response.geturl()  # pyright: ignore[reportDeprecated]
            if not _is_approved_manufacturer_url(final_url):
                raise CorpusFetchError(
                    "datasheet redirect target is not an approved manufacturer URL"
                )
            payload = response.read()
    except CorpusFetchError:
        raise
    except (OSError, URLError) as error:
        raise CorpusFetchError(f"datasheet download failed: {error}") from error

    if not payload.startswith(b"%PDF-"):
        raise CorpusFetchError("datasheet response does not begin with %PDF-; expected a PDF")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != entry.datasheet.sha256:
        raise CorpusFetchError(
            f"datasheet SHA-256 mismatch for {entry.id}: "
            f"expected {entry.datasheet.sha256}, got {digest}"
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=cache_dir,
            prefix=f".{entry.id}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o644)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a corpus datasheet from its manifest URL and verify its SHA-256."
    )
    parser.add_argument("--entry", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    print(fetch_corpus_datasheet(args.entry, args.cache))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
