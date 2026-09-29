"""Transport and response-envelope handling shared by World Bank endpoints."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Self
from urllib.parse import urlparse, urlunparse

import httpx

from .config import Settings
from .errors import WorldBankPayloadError, WorldBankRequestError, WorldBankResponseError
from .models import Page

# Live-tested 2026-09-04: real pdfurl/txturl values point at documents.worldbank.org
# over plain HTTP, and redirect between the documents/documents1 hosts.
ALLOWED_DOCUMENT_HOSTS = frozenset(
    {"documents.worldbank.org", "documents1.worldbank.org", "search.worldbank.org", "projects.worldbank.org"}
)
ALLOWED_DOCUMENT_TYPES = frozenset({"application/pdf", "text/plain", "application/octet-stream"})


class WorldBankApiClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=str(settings.world_bank_base_url).rstrip("/"),
            timeout=httpx.Timeout(settings.world_bank_timeout_seconds),
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "wb-connector-poc/0.1"},
        )
        # Downloads get their own client: redirects must be validated hop by hop, so
        # they are never followed automatically, and the Accept header is not JSON.
        self._download_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.world_bank_document_timeout_seconds),
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/pdf, text/plain", "User-Agent": "wb-connector-poc/0.1"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._download_client.aclose()

    @staticmethod
    def assert_downloadable(url: str, allowed_hosts: frozenset[str]) -> str:
        """Return an HTTPS URL on an allowed host, or raise.

        The World Bank publishes document URLs over plain HTTP. The scheme is upgraded
        rather than the check relaxed, so the request is still authenticated in transit.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise WorldBankResponseError(f"Document URL scheme {parsed.scheme!r} is not permitted", url=url)
        host = (parsed.hostname or "").lower()
        if host not in allowed_hosts:
            raise WorldBankResponseError(f"Document host {host!r} is not on the allowlist", url=url)
        if parsed.port not in (None, 80, 443):
            raise WorldBankResponseError(f"Document port {parsed.port} is not permitted", url=url)
        return urlunparse(parsed._replace(scheme="https", netloc=host))

    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str] = ALLOWED_DOCUMENT_HOSTS,
        max_bytes: int = 25_000_000,
        max_redirects: int = 5,
    ) -> tuple[bytes, str]:
        """Download a document, revalidating the allowlist on every redirect hop."""
        current = self.assert_downloadable(url, allowed_hosts)
        for _ in range(max_redirects + 1):
            try:
                async with self._download_client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WorldBankResponseError("Redirect without a Location header", url=current)
                        # Revalidating here is the whole point: the pre-request check
                        # alone would let a redirect walk off the allowlist.
                        current = self.assert_downloadable(str(response.next_request.url), allowed_hosts)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type and content_type not in ALLOWED_DOCUMENT_TYPES:
                        raise WorldBankResponseError(
                            f"Refusing document of content-type {content_type!r}", url=current
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise WorldBankResponseError(
                            f"Document declares {declared} bytes, over the {max_bytes} limit", url=current
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise WorldBankResponseError(
                                f"Document exceeded the {max_bytes} byte limit", url=current
                            )
                    return bytes(body), current
            except httpx.HTTPStatusError as exc:
                raise WorldBankResponseError(
                    f"World Bank returned HTTP {exc.response.status_code} for a document",
                    url=current,
                    status_code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise WorldBankRequestError(str(exc), url=current, attempts=1) from exc
        raise WorldBankResponseError("Too many redirects while fetching a document", url=current)

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        """Exponential backoff, but honour an explicit Retry-After when the server sends one."""
        if response is not None:
            retry_after = response.headers.get("retry-after", "").strip()
            if retry_after.isdigit():
                return min(float(retry_after), 60.0)
        return self.settings.world_bank_backoff_seconds * (2 ** (attempt - 1))

    @staticmethod
    def _describe_non_json(response: httpx.Response) -> str:
        """Explain a non-JSON body without trusting or echoing much of it.

        A network middlebox commonly answers with an HTML interstitial. Naming that
        case keeps an environment failure from being misread as an API failure, which
        docs/world-bank-api-validation.md requires.
        """
        content_type = response.headers.get("content-type", "unknown")
        prefix = response.text[:200].replace("\n", " ").strip()
        markers = ("fortiguard", "web filter", "web page blocked", "access blocked", "<html")
        interstitial = any(marker in prefix.lower() for marker in markers)
        kind = "a network filter/interstitial page" if interstitial else "a non-JSON body"
        return (
            f"Expected JSON from {response.url} but received {kind} "
            f"(HTTP {response.status_code}, content-type {content_type}): {prefix!r}"
        )

    async def get_json(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        # `format` is applied last so a caller cannot silently override the JSON contract.
        query = {**{key: value for key, value in params.items() if value is not None}, "format": "json"}
        attempts = self.settings.world_bank_max_retries
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(path, params=query)
                if response.status_code in {408, 429, 500, 502, 503, 504} and attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt, response))
                    continue
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise WorldBankResponseError(
                    f"World Bank returned HTTP {exc.response.status_code}",
                    url=str(exc.request.url),
                    status_code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                # `.request` is unset when the failure precedes the request being attached.
                url = str(exc.request.url) if exc._request is not None else f"{self._client.base_url}{path}"
                if attempt == attempts:
                    raise WorldBankRequestError(str(exc), url=url, attempts=attempt) from exc
                await asyncio.sleep(self._backoff(attempt))
                continue
            # Decoding is deterministic: a malformed body will not become valid on retry.
            try:
                payload = response.json()
            except ValueError as exc:
                raise WorldBankPayloadError(self._describe_non_json(response)) from exc
            if not isinstance(payload, dict):
                raise WorldBankPayloadError(f"Expected a JSON object response from {response.url}")
            return payload, str(response.url)
        raise AssertionError("unreachable")

    @staticmethod
    def page_from_payload(payload: dict[str, Any], record_key: str) -> Page:
        # The envelope is deliberately validated at runtime. Field names are documented in validation notes.
        container = payload.get(record_key, {})
        if isinstance(container, dict):
            records = list(container.values())
        elif isinstance(container, list):
            records = container
        else:
            raise WorldBankPayloadError(f"{record_key} is neither a mapping nor a list of records")
        if not all(isinstance(record, dict) for record in records):
            raise WorldBankPayloadError(f"{record_key} contains a non-object record")
        def number(name: str) -> int | None:
            value = payload.get(name)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        return Page(
            records=records,
            page=number("page"),
            pages=number("pages"),
            offset=number("os"),
            rows=number("rows"),
            total=number("total"),
            raw_payload=payload,
        )

    @staticmethod
    def _record_ids(page: Page) -> set[str]:
        return {str(record["id"]) for record in page.records if record.get("id") is not None}

    async def paginate(
        self, path: str, params: dict[str, Any], *, record_key: str, max_pages: int = 1000
    ) -> AsyncIterator[tuple[Page, str]]:
        """Paginate with a ``page`` parameter.

        Live-tested 2026-09-04: ``/api/v2/projects`` **ignores** ``page`` and returns
        page 1 forever. The loop is therefore driven by a local counter, stops as soon
        as a page repeats the previous page's ids, and is bounded regardless. Deriving
        the next page from the server's echoed value produced an unbounded request flood.
        """
        previous_ids: set[str] | None = None
        for page_number in range(1, max_pages + 1):
            payload, url = await self.get_json(path, {**params, "page": page_number})
            page = self.page_from_payload(payload, record_key)
            current_ids = self._record_ids(page)
            if previous_ids is not None and current_ids and current_ids == previous_ids:
                raise WorldBankPayloadError(
                    f"{path} ignored page={page_number}: it returned the same records as the "
                    "previous page. This endpoint does not support `page` pagination."
                )
            yield page, url
            if not page.records:
                return
            if page.pages is not None and page_number >= page.pages:
                return
            previous_ids = current_ids

    async def paginate_offset(
        self, path: str, params: dict[str, Any], *, record_key: str, max_pages: int = 1000
    ) -> AsyncIterator[tuple[Page, str]]:
        """Paginate APIs that document ``os`` as an offset, such as WDS v3.

        Live-tested: ``/api/procnotices`` overlaps pages — 20,000 records fetched across
        20 pages yielded 16,380 distinct ids (18.1% duplicates). The offset advances by
        the requested page size rather than by the returned record count, and duplicate
        ids are suppressed so a caller sees each notice once.
        """
        offset = int(params.get("os", 0))
        rows = int(params.get("rows", 10))
        if offset < 0 or rows < 1:
            raise ValueError("os must be non-negative and rows must be positive")
        seen: set[str] = set()
        for _ in range(max_pages):
            payload, url = await self.get_json(path, {**params, "os": offset, "rows": rows})
            page = self.page_from_payload(payload, record_key)
            if not page.records:
                return
            fresh = [record for record in page.records if str(record.get("id")) not in seen]
            seen |= self._record_ids(page)
            if fresh:
                yield page.model_copy(update={"records": fresh}), url
            offset += rows
            if page.total is not None and offset >= page.total:
                return
