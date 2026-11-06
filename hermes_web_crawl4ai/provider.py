"""Crawl4AI extraction provider and bounded crawl tool for Hermes Agent."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

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


def _is_same_site(seed_url: str, candidate_url: str) -> bool:
    """Accept the seed host and its subdomains; reject unrelated crawl results."""
    seed_host = (urlparse(seed_url).hostname or "").lower().rstrip(".")
    candidate_host = (urlparse(candidate_url).hostname or "").lower().rstrip(".")
    if not seed_host or not candidate_host:
        return False
    return candidate_host == seed_host or candidate_host.endswith(f".{seed_host}")


class Crawl4AIWebSearchProvider(WebSearchProvider):
    """Extract-only provider intended to pair with any search-only provider."""

    def __init__(self, get_config=None):
        # The context resolves settings for the active Hermes profile on every call.
        self.get_config = get_config or (lambda key, default=None: default)

    @property
    def name(self) -> str:
        return "crawl4ai"

    @property
    def display_name(self) -> str:
        return "Crawl4AI"

    def is_available(self) -> bool:
        try:
            parsed = urlparse(self._api_url())
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            )
        except ValueError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        format_value = kwargs.get("format")
        concurrency = self._bounded_int("concurrency", DEFAULT_CONCURRENCY, 1, 10)
        semaphore = asyncio.Semaphore(concurrency)

        async def extract_one(url: str) -> dict[str, Any]:
            try:
                normalized_url = normalize_url_for_request(url)
                error = await _target_error(normalized_url)
                if error:
                    return _error_document(normalized_url, error)
                async with semaphore:
                    return await self._fetch_document(normalized_url, format=format_value)
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
            "tag": "Extract backend; pair with SearXNG for a local OSS stack.",
            "env_vars": [],
        }

    def _bounded_int(self, name: str, default: int, minimum: int, maximum: int) -> int:
        value = self.get_config(name, default)
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError, OverflowError):
            return default

    def _api_url(self) -> str:
        """Return the Crawl4AI base URL from the plugin's config.yaml block."""
        configured = str(self.get_config("api_url") or get_provider_env("CRAWL4AI_URL")).strip()
        return configured.rstrip("/")

    async def _post_json(
        self, path: str, payload: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        api_url = self._api_url()
        if not api_url:
            raise ValueError(
                "Crawl4AI is not configured. Set "
                f"plugins.entries.{PLUGIN_ID}.settings.api_url in config.yaml."
            )
        parsed_url = urlparse(api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("Crawl4AI api_url must be an HTTP(S) URL with a hostname")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError(
                "Crawl4AI api_url must not contain credentials, a query, or a fragment; "
                "use CRAWL4AI_API_TOKEN"
            )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(f"{api_url}{path}", json=payload, headers=_headers())
            response.raise_for_status()
            parsed = response.json()
        if not isinstance(parsed, dict):
            raise ValueError("Crawl4AI returned a non-object JSON response")
        if parsed.get("success") is False:
            raise ValueError("Crawl4AI reported a failed request")
        return parsed

    def _truncate_document(self, document: dict[str, Any]) -> dict[str, Any]:
        limit = self._bounded_int("max_chars_per_page", DEFAULT_MAX_CHARS, 2_000, 200_000)
        result = dict(document)
        content = str(result.get("content") or "")
        if len(content) > limit:
            result["content"] = content[:limit] + f"\n\n[Truncated after {limit} characters]"
        raw = str(result.get("raw_content") or "")
        if len(raw) > limit:
            result["raw_content"] = raw[:limit]
        return result

    async def _secure_document(self, document: dict[str, Any], fallback_url: str) -> dict[str, Any]:
        url = str(document.get("url") or fallback_url)
        error = await _target_error(url)
        if error:
            return _error_document(url, error)
        document["url"] = url
        return self._truncate_document(document)

    async def _fetch_document(self, url: str, *, format: str | None = None) -> dict[str, Any]:
        """Fetch one URL through Crawl4AI's flat rendered-content endpoints."""
        timeout = self._bounded_int("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 10, 300)
        if format == "html":
            raw = await self._post_json("/html", {"url": url}, timeout=timeout)
            document = _normalize_document(raw, fallback_url=url)
        else:
            raw = await self._post_json("/md", {"url": url, "f": "fit"}, timeout=timeout)
            document = _normalize_document(raw, fallback_url=url)
            if not document.get("content") and not document.get("error"):
                raw = await self._post_json("/md", {"url": url, "f": "raw"}, timeout=timeout)
                document = _normalize_document(raw, fallback_url=url)
        if not document or not (document.get("content") or document.get("error")):
            return _error_document(url, "Crawl4AI returned no document")
        return await self._secure_document(document, url)

    async def crawl4ai_crawl_tool(self, args: dict[str, Any], **_: Any) -> str:
        """Run a bounded deep crawl without adding a core Hermes tool."""
        try:
            url = normalize_url_for_request(str(args.get("url") or "").strip())
            if not url:
                return json.dumps({"success": False, "error": "A URL is required"})
            error = await _target_error(url)
            if error:
                return json.dumps({"success": False, "error": error, "url": url})

            depth = "advanced" if args.get("depth") == "advanced" else "basic"
            max_pages = max(1, min(int(args.get("max_pages", 10)), MAX_CRAWL_PAGES))
            max_depth = 2 if depth == "advanced" else 1
            timeout = self._bounded_int("crawl_timeout_seconds", 180, 30, 300)

            # Crawl4AI 0.9+ rejects server-side deep_crawl_strategy in untrusted
            # requests. Follow links here so every page is bounded and policy-checked
            # BEFORE it is handed to the service (also works with 0.8.9).
            pending = deque([(url, 0)])
            seen = {urldefrag(url)[0]}
            results = []
            async with asyncio.timeout(timeout):
                while pending and len(results) < max_pages:
                    target, level = pending.popleft()
                    policy_error = await _target_error(target)
                    if policy_error:
                        results.append(_error_document(target, policy_error))
                        continue
                    payload = {
                        "urls": [target],
                        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
                        "crawler_config": {
                            "type": "CrawlerRunConfig",
                            "params": {"cache_mode": "bypass"},
                        },
                    }
                    try:
                        raw = await self._post_json("/crawl", payload, timeout=timeout)
                        documents = _normalize_documents(raw, fallback_url=target)
                        document = (
                            documents[0]
                            if documents
                            else _error_document(target, "Crawl4AI returned no document")
                        )
                        if not _is_same_site(url, document.get("url") or target):
                            document = _error_document(
                                target, "Blocked: crawl result left the seed site"
                            )
                        document = await self._secure_document(document, target)
                    except (httpx.HTTPError, ValueError) as exc:
                        results.append(_error_document(target, f"Crawl4AI request failed: {exc}"))
                        continue
                    results.append(document)
                    if document.get("error") or level >= max_depth:
                        continue
                    items = raw.get("results", raw.get("result", []))
                    item = items[0] if isinstance(items, list) and items else {}
                    links = item.get("links") if isinstance(item, dict) else None
                    links = links.get("internal", []) if isinstance(links, dict) else []
                    if not isinstance(links, list):
                        continue
                    for link in links:
                        href = link.get("href") if isinstance(link, dict) else link
                        if not isinstance(href, str) or not href.strip():
                            continue
                        try:
                            candidate = urldefrag(
                                normalize_url_for_request(urljoin(document["url"], href))
                            )[0]
                        except ValueError:
                            continue
                        if candidate in seen or not _is_same_site(url, candidate):
                            continue
                        # Limit the queue as well as fetches; a page may contain thousands of links.
                        if len(seen) >= max_pages:
                            break
                        seen.add(candidate)
                        pending.append((candidate, level + 1))
            return json.dumps({"results": results}, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
            safe_url = locals().get("url", str(args.get("url") or ""))
            logger.warning("Crawl4AI crawl failed for %s: %s", safe_url, exc)
            return json.dumps(
                {"success": False, "error": f"Crawl4AI crawl failed: {exc}", "url": safe_url},
                ensure_ascii=False,
            )
