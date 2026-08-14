"""Crawl4AI extraction provider and bounded crawl tool for Hermes Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from agent.web_search_provider import WebSearchProvider, get_provider_env
from tools.url_safety import async_is_safe_url, normalize_url_for_request
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-web-crawl4ai"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_CHARS = 20_000
MAX_CRAWL_PAGES = 20


# ---------------------------------------------------------------------------
# Content-quality gate + failure classification
# ---------------------------------------------------------------------------
# Crawl4AI's /md endpoint returns `success: true` for a JS-rendered bot
# challenge (Cloudflare "checking your browser", etc.) and collapses every
# non-success (404, 403, 429, PDF download) into a bare HTTP 500 with the real
# reason only in container logs. To give the agent a usable signal, classify
# results ourselves: a content gate rejects walls/empty shells, and a typed
# taxonomy (not_found / blocked / rate_limited / challenge / empty /
# unsupported_media / pdf_error / upstream_error) replaces the raw httpx string.

_MIN_CONTENT_CHARS = 60

_CHALLENGE_PATTERNS = (
    (re.compile(r"checking\s+your\s+browser", re.IGNORECASE), "challenge"),
    (re.compile(r"just\s+a\s+moment", re.IGNORECASE), "challenge"),
    (re.compile(r"verify\s+you\s+are\s+(a\s+)?human", re.IGNORECASE), "challenge"),
    (re.compile(r"enable\s+javascript\s+and\s+cookies", re.IGNORECASE), "challenge"),
    (re.compile(r"are\s+you\s+a\s+robot", re.IGNORECASE), "challenge"),
    (re.compile(r"pardon\s+our\s+interruption", re.IGNORECASE), "challenge"),
    (re.compile(r"complete\s+the\s+security\s+check", re.IGNORECASE), "challenge"),
    (re.compile(r"one\s+more\s+step", re.IGNORECASE), "challenge"),
)

_BLOCK_PATTERNS = (
    (re.compile(r"access\s+denied", re.IGNORECASE), "blocked"),
    (re.compile(r"403\s+forbidden", re.IGNORECASE), "blocked"),
    (re.compile(r"request\s+unsuccessful", re.IGNORECASE), "blocked"),
    (re.compile(r"rate\s+limit(ed)?\s+(exceeded|reached)", re.IGNORECASE), "rate_limited"),
    (re.compile(r"too\s+many\s+requests", re.IGNORECASE), "rate_limited"),
)

# 'captcha' is a strong signal but also appears in legitimate articles; only
# flag it on short pages (a challenge wall) to avoid false positives.
_CAPTCHA_RE = re.compile(r"captcha", re.IGNORECASE)
_CAPTCHA_MAX_LEN = 500

_GATE_MESSAGES = {
    "challenge": "Page returned a bot/challenge wall instead of content",
    "blocked": "Page blocked automated access",
    "rate_limited": "Page rate-limited the request",
    "empty": "Page returned no extractable content",
}


def _classify_content(content: str) -> Optional[str]:
    """Return a failure classification when ``content`` looks like a block /
    challenge wall or an empty shell rather than real page content; else None."""
    text = (content or "").strip()
    if not text:
        return "empty"
    for pattern, kind in _CHALLENGE_PATTERNS + _BLOCK_PATTERNS:
        if pattern.search(text):
            return kind
    if len(text) < _CAPTCHA_MAX_LEN and _CAPTCHA_RE.search(text):
        return "challenge"
    if len(text) < _MIN_CONTENT_CHARS:
        return "empty"
    return None


def _status_to_error_type(status: int) -> Optional[str]:
    if status in (404, 410):
        return "not_found"
    if status in (401, 403):
        return "blocked"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "upstream_error"
    return None


def _typed_error_document(url: str, error_type: str, message: str) -> dict[str, Any]:
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "metadata": {},
        "error": message,
        "error_type": error_type,
    }


_PROBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def _probe_status(url: str) -> tuple[Optional[int], Optional[str]]:
    """Best-effort HEAD probe to recover the target's real HTTP status +
    content-type (Crawl4AI's /md hides both). Never raises; a failed probe
    yields (None, None). Follows redirects so a redirect-to-PDF is detected."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            resp = await client.head(url, headers={"User-Agent": _PROBE_UA})
            return resp.status_code, resp.headers.get("content-type", "")
    except Exception:  # noqa: BLE001
        pass
    # Some servers reject HEAD (405) or hang; retry with a streamed GET that
    # reads no body.
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": _PROBE_UA}
            ) as resp:
                return resp.status_code, resp.headers.get("content-type", "")
    except Exception:  # noqa: BLE001
        return None, None


def _url_is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


async def _extract_pdf(url: str) -> dict[str, Any]:
    """Extract a PDF via Hermes' own document extractor (tools.read_extract →
    firecrawl-anydoc, lazily installed on first use). Reuses core's scanned-PDF
    coverage detection. Never raises — failures become a typed error document."""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/pdf,*/*"})
            resp.raise_for_status()
            data = resp.content
        if not data:
            return _typed_error_document(url, "empty", "PDF returned no bytes")
        from tools.read_extract import extract_document_bytes

        text = extract_document_bytes(data, url)
    except Exception as exc:  # noqa: BLE001
        return _typed_error_document(url, "pdf_error", f"PDF extraction failed: {exc}")
    if not text or not text.strip():
        return _typed_error_document(url, "empty", "PDF contained no extractable text")
    return {
        "url": url,
        "title": "",
        "content": text,
        "raw_content": text,
        "metadata": {"resolved_by": "read_extract"},
    }


async def _stealth_fetch(url: str) -> Optional[dict[str, Any]]:
    """Last-resort stealth fetch via scrapling when the content gate flags a
    challenge/block. Runs in-process (not Docker), so it also covers a downed
    Crawl4AI container. scrapling's StealthyFetcher is synchronous (patchright
    sync API), so it runs in a worker thread. Returns None on any failure."""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.scrapling", prompt=False)
    except Exception:  # noqa: BLE001 — core may not know search.scrapling yet
        pass
    try:
        from scrapling import StealthyFetcher
    except Exception:  # noqa: BLE001 — scrapling not installed / unavailable
        return None

    def _run() -> str:
        fetcher = StealthyFetcher()
        page = fetcher.fetch(
            url,
            headless=True,
            solve_cloudflare=True,
            network_idle=True,
            wait_selector="body",
            timeout=30000,
        )
        return (page.get_all_text() if page else "") or ""

    try:
        text = (await asyncio.to_thread(_run)).strip()
    except Exception:  # noqa: BLE001
        return None
    if not text or _classify_content(text):
        return None
    return {
        "url": url,
        "title": "",
        "content": text,
        "raw_content": text,
        "metadata": {"resolved_by": "scrapling_stealth"},
    }


@lru_cache(maxsize=1)
def _plugin_config() -> dict[str, Any]:
    """Read this plugin's non-secret settings from config.yaml."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:  # noqa: BLE001 - stripped/early host contexts fail closed
        return {}
    entries = (config.get("plugins") or {}).get("entries") or {}
    entry = entries.get(PLUGIN_ID) or {}
    settings = entry.get("config") or {}
    return settings if isinstance(settings, dict) else {}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _plugin_config().get(name, default)
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _api_url() -> str:
    """Return the Crawl4AI base URL from the plugin's config.yaml block."""
    configured = str(_plugin_config().get("api_url") or "").strip()
    return configured.rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = get_provider_env("CRAWL4AI_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return {key: item for key, item in value.__dict__.items() if not key.startswith("_")}
        except Exception:  # noqa: BLE001
            pass
    return value


def _normalize_document(item: Any, fallback_url: str = "") -> dict[str, Any]:
    """Normalize one Crawl4AI response object to Hermes' extract contract."""
    plain = _to_plain(item)
    if not isinstance(plain, dict):
        return {}

    metadata = _to_plain(plain.get("metadata"))
    if not isinstance(metadata, dict):
        metadata = {}

    markdown = _to_plain(plain.get("markdown"))
    if isinstance(markdown, dict):
        content = (
            markdown.get("fit_markdown")
            or markdown.get("raw_markdown")
            or markdown.get("markdown_with_citations")
            or markdown.get("references_markdown")
            or ""
        )
        raw_content = (
            markdown.get("raw_markdown")
            or markdown.get("markdown_with_citations")
            or markdown.get("fit_markdown")
            or ""
        )
    else:
        content = markdown if isinstance(markdown, str) else ""
        raw_content = content
    content = (
        content
        or plain.get("fit_markdown")
        or plain.get("raw_markdown")
        or plain.get("extracted_content")
        or plain.get("content")
        or plain.get("cleaned_html")
        or plain.get("html")
        or ""
    )
    raw_content = (
        raw_content
        or plain.get("raw_markdown")
        or plain.get("raw_content")
        or plain.get("html")
        or content
        or ""
    )
    url = (
        plain.get("redirected_url")
        or plain.get("url")
        or metadata.get("sourceURL")
        or metadata.get("url")
        or fallback_url
    )
    title = plain.get("title") or metadata.get("title") or ""

    result: dict[str, Any] = {
        "url": str(url or ""),
        "title": str(title or ""),
        "content": str(content or ""),
        "raw_content": str(raw_content or ""),
        "metadata": metadata,
    }
    if plain.get("success") is False:
        result["content"] = ""
        result["raw_content"] = ""
        result["error"] = str(
            plain.get("error")
            or plain.get("error_message")
            or plain.get("message")
            or "Crawl4AI request failed"
        )
    elif plain.get("error") and not content:
        result["error"] = str(plain["error"])
    return result


def _normalize_documents(response: Any, fallback_url: str = "") -> list[dict[str, Any]]:
    plain = _to_plain(response)
    if not isinstance(plain, dict):
        return []

    candidates = plain.get("results")
    if not isinstance(candidates, list):
        candidates = plain.get("result")
    if not isinstance(candidates, list):
        candidates = []

    documents = [
        normalized
        for item in candidates
        if (normalized := _normalize_document(item, fallback_url=fallback_url))
    ]
    if not documents and any(
        key in plain for key in ("markdown", "html", "content", "raw_markdown")
    ):
        normalized = _normalize_document(plain, fallback_url=fallback_url)
        if normalized:
            documents.append(normalized)
    return documents


async def _post_json(path: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    api_url = _api_url()
    if not api_url:
        raise ValueError(
            "Crawl4AI is not configured. Set "
            f"plugins.entries.{PLUGIN_ID}.config.api_url in config.yaml."
        )
    parsed_url = urlparse(api_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("Crawl4AI api_url must be an HTTP(S) URL with a hostname")
    if parsed_url.username or parsed_url.password:
        raise ValueError(
            "Crawl4AI api_url must not contain credentials; use CRAWL4AI_API_TOKEN"
        )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(f"{api_url}{path}", json=payload, headers=_headers())
        response.raise_for_status()
        parsed = response.json()
    if not isinstance(parsed, dict):
        raise ValueError("Crawl4AI returned a non-object JSON response")
    return parsed


async def _target_error(url: str) -> str | None:
    """Return a policy error for a model/backend-provided target URL."""
    if not await async_is_safe_url(url):
        return "Blocked: URL targets a private or internal network address"
    blocked = check_website_access(url)
    if blocked:
        return blocked["message"]
    return None


def _error_document(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error}


def _truncate_document(document: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int("max_chars_per_page", DEFAULT_MAX_CHARS, 2_000, 200_000)
    result = dict(document)
    content = str(result.get("content") or "")
    if len(content) > limit:
        result["content"] = content[:limit] + f"\n\n[Truncated after {limit} characters]"
    raw = str(result.get("raw_content") or "")
    if len(raw) > limit:
        result["raw_content"] = raw[:limit]
    return result


async def _secure_document(document: dict[str, Any], fallback_url: str) -> dict[str, Any]:
    url = str(document.get("url") or fallback_url)
    error = await _target_error(url)
    if error:
        return _error_document(url, error)
    document["url"] = url
    return _truncate_document(document)


def _is_same_site(seed_url: str, candidate_url: str) -> bool:
    """Accept the seed host and its subdomains; reject unrelated crawl results."""
    seed_host = (urlparse(seed_url).hostname or "").lower().rstrip(".")
    candidate_host = (urlparse(candidate_url).hostname or "").lower().rstrip(".")
    if not seed_host or not candidate_host:
        return False
    return candidate_host == seed_host or candidate_host.endswith(f".{seed_host}")


async def _crawl4ai_fetch(url: str, *, format: str | None = None) -> dict[str, Any]:
    """Fetch one URL through Crawl4AI's flat rendered-content endpoints."""
    timeout = _bounded_int("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 10, 300)
    if format == "html":
        raw = await _post_json("/html", {"url": url}, timeout=timeout)
        document = _normalize_document(raw, fallback_url=url)
    else:
        raw = await _post_json("/md", {"url": url, "f": "fit"}, timeout=timeout)
        document = _normalize_document(raw, fallback_url=url)
        if not document.get("content") and not document.get("error"):
            raw = await _post_json("/md", {"url": url, "f": "raw"}, timeout=timeout)
            document = _normalize_document(raw, fallback_url=url)
    if not document:
        return _error_document(url, "Crawl4AI returned no document")
    return await _secure_document(document, url)


async def _fetch_document(url: str, *, format: str | None = None) -> dict[str, Any]:
    """Fetch one URL with a layered strategy and typed failure classification:

      1. obvious-PDF extension → Hermes' document extractor (read_extract)
      2. best-effort HEAD probe → PDF-by-content-type + early-out on 404/410
      3. Crawl4AI /md (fit → raw) or /html
      4. content-quality gate → typed error, with a scrapling stealth
         escalation for challenge/blocked walls
    """
    if _url_is_pdf(url):
        return await _extract_pdf(url)

    probe_status, content_type = await _probe_status(url)

    if (content_type or "").lower().startswith("application/pdf"):
        return await _extract_pdf(url)

    if probe_status in (404, 410):
        return _typed_error_document(
            url, "not_found", f"URL returned HTTP {probe_status} (not found)"
        )

    try:
        document = await _crawl4ai_fetch(url, format=format)
    except Exception as exc:  # noqa: BLE001 — Crawl4AI 500 / connection / timeout
        if probe_status:
            error_type = _status_to_error_type(probe_status) or "crawl4ai_error"
            message = f"Target returned HTTP {probe_status}"
        else:
            error_type = "crawl4ai_error"
            message = f"Crawl4AI extract failed: {exc}"
        return _typed_error_document(url, error_type, message)

    if document.get("error"):
        # Already a typed per-URL failure (policy block, success=False, etc.).
        return document

    gate = _classify_content(document.get("content") or "")
    if not gate:
        return document

    if gate in ("challenge", "blocked"):
        stealth = await _stealth_fetch(url)
        if stealth:
            return stealth

    return _typed_error_document(
        url, gate, _GATE_MESSAGES.get(gate, "Page content failed quality checks")
    )


class Crawl4AIWebSearchProvider(WebSearchProvider):
    """Extract-only provider intended to pair with Hermes' bundled SearXNG plugin."""

    @property
    def name(self) -> str:
        return "crawl4ai"

    @property
    def display_name(self) -> str:
        return "Crawl4AI"

    def is_available(self) -> bool:
        return bool(_api_url())

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        format_value = kwargs.get("format")
        concurrency = _bounded_int("concurrency", DEFAULT_CONCURRENCY, 1, 10)
        semaphore = asyncio.Semaphore(concurrency)

        async def extract_one(url: str) -> dict[str, Any]:
            try:
                normalized_url = normalize_url_for_request(url)
                error = await _target_error(normalized_url)
                if error:
                    return _error_document(normalized_url, error)
                async with semaphore:
                    return await _fetch_document(normalized_url, format=format_value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed URL must not fail the batch
                safe_url = locals().get("normalized_url", str(url))
                logger.warning("Crawl4AI extract failed for %s: %s", safe_url, exc)
                return _error_document(safe_url, f"Crawl4AI extract failed: {exc}")

        return list(await asyncio.gather(*(extract_one(url) for url in urls)))

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Crawl4AI",
            "badge": "free · self-hosted",
            "tag": "Extract backend; pair with bundled SearXNG for a local OSS stack.",
            "env_vars": [],
        }


async def crawl4ai_crawl_tool(args: dict[str, Any], **_: Any) -> str:
    """Run a bounded same-site crawl by walking links client-side.

    Crawl4AI 0.9.x rejects ``deep_crawl_strategy`` from untrusted (network)
    request bodies, so deep crawling is implemented here as a client-side BFS
    over the flat ``/md`` endpoint instead of a server-side deep-crawl strategy.
    """
    try:
        import re

        url = normalize_url_for_request(str(args.get("url") or "").strip())
        if not url:
            return json.dumps({"success": False, "error": "A URL is required"})
        error = await _target_error(url)
        if error:
            return json.dumps({"success": False, "error": error, "url": url})

        max_pages = max(1, min(int(args.get("max_pages", 10)), MAX_CRAWL_PAGES))
        max_depth = 2 if args.get("depth") == "advanced" else 1
        timeout = _bounded_int("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 10, 300)

        link_re = re.compile(r"https?://[^\s<>\"')\]]+")

        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(url, 0)]
        documents: list[dict[str, Any]] = []

        while queue and len(documents) < max_pages:
            current, depth = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)

            try:
                raw = await _post_json("/md", {"url": current, "f": "fit"}, timeout=timeout)
                document = _normalize_document(raw, fallback_url=current)
                if not document.get("content") and not document.get("error"):
                    raw = await _post_json("/md", {"url": current, "f": "raw"}, timeout=timeout)
                    document = _normalize_document(raw, fallback_url=current)
            except Exception as exc:  # noqa: BLE001 - one failed page must not kill the crawl
                document = _error_document(current, f"Crawl4AI fetch failed: {exc}")

            documents.append(await _secure_document(document, current))

            if depth < max_depth:
                text = str(document.get("content") or document.get("raw_content") or "")
                for match in link_re.finditer(text):
                    candidate = match.group(0).rstrip(".,;!?)")
                    if candidate not in seen and _is_same_site(url, candidate):
                        queue.append((candidate, depth + 1))

        return json.dumps({"results": documents}, ensure_ascii=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
        safe_url = locals().get("url", str(args.get("url") or ""))
        logger.warning("Crawl4AI crawl failed for %s: %s", safe_url, exc)
        return json.dumps(
            {"success": False, "error": f"Crawl4AI crawl failed: {exc}", "url": safe_url},
            ensure_ascii=False,
        )
