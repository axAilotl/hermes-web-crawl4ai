import httpx
import pytest

from hermes_web_crawl4ai import provider as module
from hermes_web_crawl4ai.provider import Crawl4AIWebSearchProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(module, "get_provider_env", lambda key: "test-token")
    return Crawl4AIWebSearchProvider({"api_url": "http://localhost:11235/base"}.get)


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_post_uses_configured_path_and_auth(monkeypatch, provider):
    def respond(request):
        assert request.url.path == "/base/md"
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.content == b'{"url":"https://example.com"}'
        return httpx.Response(200, json={"markdown": "hello"})

    mock_http(monkeypatch, respond)
    assert await provider._post_json("/md", {"url": "https://example.com"}, timeout=10) == {
        "markdown": "hello"
    }


async def test_auth_is_not_forwarded_on_redirect(monkeypatch, provider):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example"})

    mock_http(monkeypatch, respond)
    with pytest.raises(httpx.HTTPStatusError):
        await provider._post_json("/md", {}, timeout=10)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [[], {"success": False, "error": "server error"}])
async def test_invalid_or_failed_envelopes_are_errors(monkeypatch, provider, body):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(ValueError):
        await provider._post_json("/crawl", {}, timeout=10)


async def test_markdown_fallback_and_html(monkeypatch, provider):
    async def allow(url):
        return None

    monkeypatch.setattr(module, "_target_error", allow)
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/html"):
            return httpx.Response(200, json={"html": "<p>hello</p>"})
        if b'"f":"fit"' in request.content:
            return httpx.Response(200, json={"markdown": ""})
        return httpx.Response(200, json={"markdown": "raw text"})

    mock_http(monkeypatch, respond)
    assert (await provider.extract(["https://example.com"]))[0]["content"] == "raw text"
    assert (await provider.extract(["https://example.com"], format="html"))[0][
        "content"
    ] == "<p>hello</p>"
    assert len(calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.com",
        "https://example.com?token=secret",
        "http://[bad",
    ],
)
async def test_invalid_service_urls_fail_before_http(monkeypatch, url):
    provider = Crawl4AIWebSearchProvider({"api_url": url}.get)

    def no_http(**kwargs):
        pytest.fail("invalid URL opened an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", no_http)
    assert not provider.is_available()
    with pytest.raises(ValueError):
        await provider._post_json("/md", {}, timeout=10)
