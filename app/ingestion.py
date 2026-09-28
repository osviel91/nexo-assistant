from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from pypdf import PdfReader


class IngestionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class CanonicalSpan:
    start_offset: int
    end_offset: int
    source_type: str
    source_location: dict[str, Any]


@dataclass(frozen=True)
class CanonicalDocumentData:
    title: str
    content: str
    content_type: str
    source_type: str
    spans: tuple[CanonicalSpan, ...] = ()
    language: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class SourceAdapter(Protocol):
    name: str

    def can_handle(self, source: Any) -> bool: ...

    def extract(self, source: Any, payload: bytes | None) -> CanonicalDocumentData: ...


def _source_mime(source: Any) -> str:
    metadata = json.loads(source["metadata"] or "{}") if isinstance(source["metadata"], str) else source["metadata"]
    return str(metadata.get("mime", "")).lower().split(";", 1)[0]


def _file_name(source: Any) -> str:
    metadata = json.loads(source["metadata"] or "{}") if isinstance(source["metadata"], str) else source["metadata"]
    return str(metadata.get("filename", "")).lower()


def _text_span(content: str, source_type: str, location: dict[str, Any]) -> CanonicalSpan:
    return CanonicalSpan(0, len(content), source_type, location)


class TextAdapter:
    name = "text"

    def can_handle(self, source: Any) -> bool:
        return _source_mime(source) == "text/plain" or _file_name(source).endswith(".txt")

    def extract(self, source: Any, payload: bytes | None) -> CanonicalDocumentData:
        if payload is None:
            raise IngestionError("MISSING_SOURCE_CONTENT", "Source content is unavailable")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise IngestionError("INVALID_UTF8", "Text source is not valid UTF-8") from None
        return CanonicalDocumentData(source["title"], content, "text/plain", "text", (_text_span(content, "text", {"line_start": 1, "line_end": content.count("\n") + 1}),))


class MarkdownAdapter:
    name = "markdown"

    def can_handle(self, source: Any) -> bool:
        return _source_mime(source) in {"text/markdown", "text/x-markdown"} or _file_name(source).endswith(('.md', '.markdown'))

    def extract(self, source: Any, payload: bytes | None) -> CanonicalDocumentData:
        if payload is None:
            raise IngestionError("MISSING_SOURCE_CONTENT", "Source content is unavailable")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise IngestionError("INVALID_UTF8", "Markdown source is not valid UTF-8") from None
        spans = []
        for match in re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)\s*$", content):
            spans.append(CanonicalSpan(match.start(), match.end(), "markdown", {"heading": match.group(2).strip(), "level": len(match.group(1))}))
        spans.append(_text_span(content, "markdown", {"document": "body"}))
        return CanonicalDocumentData(source["title"], content, "text/markdown", "markdown", tuple(spans))


class PdfAdapter:
    name = "pdf"

    def can_handle(self, source: Any) -> bool:
        return _source_mime(source) == "application/pdf" or _file_name(source).endswith(".pdf")

    def extract(self, source: Any, payload: bytes | None) -> CanonicalDocumentData:
        if payload is None:
            raise IngestionError("MISSING_SOURCE_CONTENT", "Source content is unavailable")
        try:
            reader = PdfReader(BytesIO(payload))
            pages = []
            spans = []
            offset = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if not text:
                    continue
                if pages:
                    pages.append("\n\n")
                    offset += 2
                start = offset
                pages.append(text)
                offset += len(text)
                spans.append(CanonicalSpan(start, offset, "pdf", {"page": number}))
            content = "".join(pages)
        except Exception:
            raise IngestionError("MALFORMED_PDF", "PDF could not be read") from None
        if not content.strip():
            raise IngestionError("EMPTY_PDF_EXTRACTION", "PDF contains no extractable text")
        return CanonicalDocumentData(source["title"], content, "text/plain", "pdf", tuple(spans), metadata={"page_count": len(reader.pages)})


class _PageTextParser(HTMLParser):
    ignored = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self._ignored = 0
        self._heading: str | None = None
        self._heading_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.ignored:
            self._ignored += 1
        if re.fullmatch(r"h[1-6]", tag) and not self._ignored:
            self._heading = tag
            self._heading_text = []

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch(r"h[1-6]", tag) and self._heading == tag:
            heading = " ".join("".join(self._heading_text).split())
            if heading:
                self.headings.append((heading, tag))
            self._heading = None
        if tag in self.ignored and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._heading:
            self._heading_text.append(data)
        if data.strip():
            self.parts.append(html.unescape(data.strip()))


class WebAdapter:
    name = "web"

    def __init__(self, fetch: Callable[[str], tuple[str, bytes, str]] | None = None):
        self.fetch = fetch or fetch_url

    def can_handle(self, source: Any) -> bool:
        return source["type"] == "web"

    def extract(self, source: Any, payload: bytes | None = None) -> CanonicalDocumentData:
        url = json.loads(source["metadata"] or "{}").get("url", "")
        final_url, body, content_type = self.fetch(url)
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}:
            raise IngestionError("UNSUPPORTED_WEB_CONTENT_TYPE", "Web response is not a supported text document")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise IngestionError("INVALID_UTF8", "Web response is not valid UTF-8") from None
        if content_type in {"text/plain", "text/markdown"}:
            data = TextAdapter().extract({**dict(source), "title": source["title"]}, body) if content_type == "text/plain" else MarkdownAdapter().extract({**dict(source), "title": source["title"]}, body)
            return CanonicalDocumentData(data.title, data.content, data.content_type, "web", data.spans, metadata={"url": final_url})
        parser = _PageTextParser()
        try:
            parser.feed(text)
        except Exception:
            raise IngestionError("INVALID_HTML", "Web response is not valid HTML") from None
        content = "\n\n".join(parser.parts).strip()
        if not content:
            raise IngestionError("EMPTY_WEB_EXTRACTION", "Web page contains no extractable text")
        spans = [_text_span(content, "web", {"url": final_url})]
        search_from = 0
        for heading, level in parser.headings:
            start = content.find(heading, search_from)
            if start >= 0:
                spans.append(CanonicalSpan(start, start + len(heading), "web", {"url": final_url, "heading": heading, "level": int(level[1:])}))
                search_from = start + len(heading)
        return CanonicalDocumentData(source["title"], content, "text/plain", "web", tuple(spans), metadata={"url": final_url, "headings": [h for h, _ in parser.headings]})


def _forbidden_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
    except ValueError:
        return False


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise IngestionError("UNSUPPORTED_URL", "URL must use HTTP(S) without credentials")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError:
        raise IngestionError("DNS_RESOLUTION_FAILED", "URL host could not be resolved") from None
    if not addresses or any(_forbidden_ip(address) for address in addresses):
        raise IngestionError("PRIVATE_NETWORK_URL", "URL resolves to a forbidden network")


def fetch_url(url: str, max_bytes: int = 5 * 1024 * 1024, max_redirects: int = 3) -> tuple[str, bytes, str]:
    current = url
    timeout = httpx.Timeout(10.0, connect=5.0, read=10.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                validate_public_url(current)
                response = client.get(current)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise IngestionError("INVALID_REDIRECT", "Redirect has no destination")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}:
                    raise IngestionError("UNSUPPORTED_WEB_CONTENT_TYPE", "Web response is not a supported text document")
                body = response.content
                if len(body) > max_bytes:
                    raise IngestionError("WEB_RESPONSE_TOO_LARGE", "Web response exceeds the ingestion limit")
                return str(response.url), body, content_type
    except IngestionError:
        raise
    except httpx.TimeoutException:
        raise IngestionError("WEB_TIMEOUT", "Web request timed out") from None
    except httpx.HTTPError:
        raise IngestionError("WEB_FETCH_FAILED", "Web document could not be fetched") from None
    raise IngestionError("REDIRECT_LIMIT", "Web redirect limit exceeded")


class NotebookIngestionService:
    def __init__(self, repository: Any, storage_root: Path, now: Callable[[], str], max_bytes: int = 15 * 1024 * 1024, adapters: list[SourceAdapter] | None = None):
        self.repository, self.storage_root, self.now, self.max_bytes = repository, storage_root, now, max_bytes
        self.adapters = adapters or [TextAdapter(), MarkdownAdapter(), PdfAdapter(), WebAdapter()]

    def ingest(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        source = self.repository.source(notebook_id, source_id)
        if source is None:
            raise LookupError(source_id)
        self.repository.set_source_status(source_id, "extracting")
        adapter = next((item for item in self.adapters if item.can_handle(source)), None)
        if adapter is None:
            return self._fail(source_id, "UNSUPPORTED_MIME_TYPE", "Source MIME type is not supported", None)
        self.repository.set_source_status(source_id, "extracting", adapter.name)
        started = time.monotonic()
        try:
            payload = None
            if source["type"] == "file":
                path = self.storage_root / source_id[:2] / source_id
                if (not path.is_file() or path.is_symlink() or path.parent.parent != self.storage_root
                        or path.resolve().parent.parent != self.storage_root.resolve() or path.name != source_id):
                    raise IngestionError("SOURCE_CONTENT_UNAVAILABLE", "Source content is unavailable")
                payload = path.read_bytes()
                if len(payload) > self.max_bytes:
                    raise IngestionError("INPUT_TOO_LARGE", "Source exceeds the ingestion limit")
            data = adapter.extract(source, payload)
            if not data.content.strip():
                raise IngestionError("EMPTY_EXTRACTION", "Source contains no extractable text")
            result = self.repository.replace_canonical(source, data)
            duration = round((time.monotonic() - started) * 1000, 2)
            self.repository.set_source_status(source_id, "ready", adapter.name, duration, len(data.content), None, None)
            return result
        except IngestionError as error:
            return self._fail(source_id, error.code, error.message, adapter.name, round((time.monotonic() - started) * 1000, 2))
        except Exception:
            return self._fail(source_id, "EXTRACTION_FAILED", "Source extraction failed", adapter.name, round((time.monotonic() - started) * 1000, 2))

    def _fail(self, source_id: str, code: str, message: str, adapter: str | None, duration: float | None = None) -> dict[str, Any]:
        self.repository.set_source_status(source_id, "failed", adapter, duration, None, code, message)
        return {"status": "failed", "error_code": code, "error_message": message}
