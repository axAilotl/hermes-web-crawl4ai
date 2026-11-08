# Hermes Web Crawl4AI

Web extraction and bounded website crawling through a self-hosted Crawl4AI REST service.
A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin.

## Requirements

- Hermes **0.23.0 or newer**, using its supported Python runtime (currently 3.14).
- A reachable Crawl4AI server. It can run on the same machine, another computer,
  or a private server. The plugin does not install the server.
- HTTP(S) access from the machine running Hermes to that service.

The plugin uses Python and HTTP; it has no desktop, shell, distro, username,
absolute-path, or local-container dependency. Its service URL is always supplied
by the user. Linux, macOS, and Windows are covered by the CI matrix.

## Install

```sh
hermes plugins install axAilotl/hermes-web-crawl4ai --enable
```

Accept Hermes' dependency prompt. For unattended installation, add `--yes-deps`.
This is a direct GitHub install; catalog inclusion requires separate maintainer review.

Configure the active Hermes profile. If you use profiles, select the intended
one first, or set `HERMES_HOME` to its directory. The default is `~/.hermes`.
Start a new Hermes session after installing, and ensure the standard `web`
toolset is enabled in `hermes tools`.

## Configure

Merge this into your active profile's `config.yaml`, preserving other entries:

```yaml
plugins:
  enabled:
    - hermes-web-crawl4ai
  entries:
    hermes-web-crawl4ai:
      settings:
        api_url: http://localhost:11235

web:
  extract_backend: crawl4ai-bounded
```

Replace `localhost` when the service runs elsewhere. If Hermes itself runs in
Docker, `localhost` means the Hermes container, not your computer; use a shared
Docker network service name or an address reachable from that container.

`CRAWL4AI_URL` is a fallback when `api_url` is unset. Store the server's Bearer
token as `CRAWL4AI_API_TOKEN` in the active profile's `.env` or secret source,
or enter it in the plugin's masked API token setting. Never put it in the URL.

**Crawl4AI 0.9+ requires a token by default.** Set the same token on the server
and in Hermes. Without a server token, the official container binds only to
its own loopback interface and published ports are unreachable. Follow the
[official server setup](https://docs.crawl4ai.com/core/self-hosting/).
Unauthenticated older servers are supported when explicitly configured that way.

Optional settings under `plugins.entries.hermes-web-crawl4ai.settings`:

| Setting | Default | Bounds |
| --- | --- | --- |
| `timeout_seconds` | 120 | 10–300 |
| `crawl_timeout_seconds` | 180 | 30–300, entire crawl |
| `concurrency` | 4 | 1–10 simultaneous extraction requests |
| `max_chars_per_page` | 20000 | 2000–200000 |

`web_extract` uses this provider for individual URLs. The separate
`crawl4ai_crawl` tool follows links breadth-first under the seed host and its
subdomains: `basic` permits one link level, `advanced` two, and `max_pages`
permits 1–20 total pages. Enable its `crawl4ai` toolset when needed.

The plugin follows links itself using single-page `/crawl` calls. It does not
send the `deep_crawl_strategy` setting rejected by Crawl4AI 0.9+'s REST trust
boundary. Every queued URL is checked against Hermes' URL and website policies
before fetching; returned redirect URLs are checked again. One failed page
becomes an error entry. Cancellation propagates, and the overall crawl has a deadline.

The extraction backend ID is `crawl4ai-bounded`, distinct from the existing
catalog plugin's `crawl4ai` backend. Select `web.extract_backend` explicitly
when both are installed. This plugin also provides bounded crawling and
profile settings.

## Use both plugins together

Install each independently:

```sh
hermes plugins install axAilotl/hermes-web-searxng --enable
hermes plugins install axAilotl/hermes-web-crawl4ai --enable
```

Merge the following configuration with your existing profile:

```yaml
plugins:
  enabled:
    - hermes-web-searxng
    - hermes-web-crawl4ai
  entries:
    hermes-web-searxng:
      settings:
        api_url: http://localhost:8080
    hermes-web-crawl4ai:
      settings:
        api_url: http://localhost:11235
web:
  search_backend: searxng-local
  extract_backend: crawl4ai-bounded
```

Neither plugin requires the other. You can pair either with another provider.

## Privacy and network behavior

The plugin sends requests only to the configured service. SearXNG forwards
queries to its enabled search engines; Crawl4AI fetches the requested pages.
These services determine where the requests ultimately go. The plugins send
no telemetry, run no shell commands or background services, and persist no
credentials or page content themselves. Hermes may retain tool results in its
normal conversation history. Credentials are resolved through Hermes' active
profile API. HTTP redirects from the service endpoint are refused so its Bearer
token is not forwarded to a different endpoint.

Use HTTPS when connecting across an untrusted network. The service may be on a
private address; model-provided crawl targets remain subject to Hermes' URL
policy. Crawl4AI's own redirect/subresource protections must also be configured:
checking a returned URL cannot undo a request the remote browser already made.

## Testing and development

CI tests Hermes v0.23.0 on Linux, macOS, and Windows, plus Hermes main on Linux.
It exercises HTTP request/response contracts, real plugin discovery from a
copied directory, backend selection, manifest validation, and a built wheel
installed outside the source import path.

For development with a working Hermes environment, install `.[dev]` there.
For an isolated test harness, use a fresh Python 3.14 virtual environment and:

```sh
git clone --branch v0.23.0 --depth 1 https://github.com/NousResearch/hermes-agent.git ../hermes-agent
python -m pip install --no-deps -e ../hermes-agent
python -m pip install -r tests/requirements-host.txt '.[dev]' build
python -m pytest -q
ruff check .
hermes plugins doctor . --ci
hermes plugins validate .
python -m build
```

The minimal host requirements are for plugin contract tests, not a complete
Hermes agent installation. Normal users install Hermes using its official
installer and then install this plugin; they should not rebuild Hermes with pip.

To run the real-service tests with Docker available:

```sh
python tests/run_live.py
```

This pulls a pinned official service image, generates disposable credentials,
binds a random localhost port, uses a temporary Hermes home, exercises actual
HTTP calls, and removes the container afterward. It does not use your configured
server or credentials. External network access is needed for search engines and
test pages. The GitHub **Live service test** workflow runs the same command on
request; external services can fail independently of the plugin.

The live runner pins Crawl4AI 0.9.2. `CRAWL4AI_TEST_URL` and
`CRAWL4AI_API_TOKEN` can instead point `tests/test_live_crawl4ai.py` at your own
disposable server. Historical 0.8.9 compatibility is maintained for the same
single-page endpoints.

## Migration and compatibility

The extraction backend is now `crawl4ai-bounded` to avoid a collision with the
existing catalog plugin. When upgrading, change `web.extract_backend: crawl4ai`
(or `web.backend: crawl4ai`) to `crawl4ai-bounded` in your active profile. The
`crawl4ai` ID is not registered as an alias, so it remains available to the other
plugin. Tool `crawl4ai_crawl`, toolset `crawl4ai`, and the existing secret name
are unchanged. Settings use `ctx.get_config()` and the `settings` subtree;
Hermes accepts the old `config` subtree as a fallback. `settings` wins when both
provide a key. Configuration is read when used rather than cached across profiles.

The native plugin uses `plugin.yaml`, `register(ctx)`, declared dependencies,
a settings schema, and the web provider API. Plugin validation currently warns
about imports of Hermes' URL normalization, async URL safety, and website-policy
helpers. They are retained to preserve host policy enforcement; CI checks both
the supported release and main for compatibility. No Hermes functions or registries
are patched. This dependency is disclosed for catalog review.

This is the standalone continuation of the work in
[NousResearch/hermes-agent#6325](https://github.com/NousResearch/hermes-agent/pull/6325),
not a fork of the other community Crawl4AI plugin.

## License

MIT
