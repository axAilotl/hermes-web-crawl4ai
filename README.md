# Hermes Web Crawl4AI

Standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin
for private, self-hosted web extraction with Crawl4AI.

This is the standalone continuation of the Crawl4AI work from
[NousResearch/hermes-agent#6325](https://github.com/NousResearch/hermes-agent/pull/6325).
It intentionally makes no changes to Hermes core and is not listed in a Hermes
plugin registry while compatibility testing is underway.

## What it provides

- `crawl4ai`, an extract-only `WebSearchProvider` used by Hermes' existing
  `web_extract` tool.
- `crawl4ai_crawl`, an optional plugin-local tool for bounded deep crawling.
- Compatibility with Crawl4AI's flat `/md` and `/html` endpoints and its
  `/crawl` response envelopes.
- Hermes URL-safety and website-policy checks on requested and returned URLs.

The companion `hermes-web-searxng` plugin provides search independently. Its
backend ID is `searxng-local`; Hermes' bundled `searxng` also remains usable.
Neither standalone plugin depends on the other.

## Status

Alpha and intended for direct testing. Do not depend on the current API as a
stable release yet. The initial target is the security-hardened Crawl4AI
v0.8.9 REST service.

Validated locally on 2026-07-12 against the pinned Crawl4AI 0.8.9 container:
real `/md`, `/html`, and typed bounded `/crawl` requests all pass. Broader
deployment testing is still wanted before registry or catalog promotion.

## Install the updated local checkout

This checkout contains version 0.2.0. Until these changes are published, install
from the local repository (replace the source path on another machine):

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins"
git clone /home/vega/Projects/hermes-web-crawl4ai "$HERMES_HOME/plugins/hermes-web-crawl4ai"
hermes plugins enable hermes-web-crawl4ai
```

If already installed, update that checkout from this repository instead of
cloning over it. Enabling uses Hermes' dependency admission workflow. These
instructions apply to the selected profile; use its `HERMES_HOME` explicitly.

## Published version

The existing GitHub repository still distributes the previously published version
until the local changes are pushed:


```bash
hermes plugins install axAilotl/hermes-web-crawl4ai --enable
```

The plugin can also be cloned directly to:

```text
~/.hermes/plugins/hermes-web-crawl4ai/
```

Then enable it:

```bash
hermes plugins enable hermes-web-crawl4ai
```

## Configure

Put the non-secret Crawl4AI service URL in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-web-crawl4ai
  entries:
    hermes-web-crawl4ai:
      settings:
        api_url: http://localhost:11235
        timeout_seconds: 120
        crawl_timeout_seconds: 180
        concurrency: 4
        max_chars_per_page: 20000

web:
  search_backend: searxng-local
  extract_backend: crawl4ai
```

Install and enable `hermes-web-searxng` separately for `searxng-local`, or set
`search_backend: searxng` to use Hermes' bundled provider. Configure that
provider's URL separately. `CRAWL4AI_URL` is an optional fallback when `api_url`
is unset; explicit plugin settings take precedence.

If the Crawl4AI instance requires authentication, put only its secret token in
`~/.hermes/.env`:

```dotenv
CRAWL4AI_API_TOKEN=replace-me
```

Start a new Hermes session after installing or enabling the plugin. Run `hermes tools` and
ensure the standard `web` toolset is enabled. Enable the `crawl4ai` plugin
toolset too if you want the model to see `crawl4ai_crawl`.

## Behavior

For known URLs, Hermes continues to call its stable core tool:

```text
web_extract -> Crawl4AIWebSearchProvider.extract()
```

The plugin-local `crawl4ai_crawl` tool is reserved for cases where multiple
linked pages under one site are needed. Its limits are deliberate:

- `basic`: maximum link depth 1
- `advanced`: maximum link depth 2
- at most 20 pages per call
- Crawl4AI receives `include_external: false`; unrelated returned hosts are rejected again by the plugin
- configurable per-page character cap, maximum 200,000
- public HTTP(S) targets only unless the user's Hermes URL-safety policy allows otherwise

Hermes validates the seed and returned URLs, while Crawl4AI performs the
actual fetches. Use a current security-patched Crawl4AI release: post-response
filtering cannot undo a request already made by an outdated or misconfigured
Crawl4AI server.

## Local development

Use a current Hermes checkout as the host contract:

```bash
git clone https://github.com/NousResearch/hermes-agent.git ../hermes-agent
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ../hermes-agent
pip install -e '.[dev]'
pytest -q
ruff check .
hermes plugins doctor . --ci
hermes plugins validate .
```

The test suite includes a real Hermes `PluginManager` discovery test, not just
mocked registration. `tests/test_live_crawl4ai.py` is an opt-in real-service
suite; set `CRAWL4AI_TEST_URL` to run it. The GitHub workflow also exposes a
manual containerized live-test job.

Live service validation is intentionally manual during the alpha period. Point
`api_url` at a disposable Crawl4AI instance, then verify:

1. `web_extract` returns markdown for one and several public URLs.
2. HTML extraction works when Hermes requests `format=html`.
3. A failed URL returns a per-URL error without failing the whole batch.
4. `crawl4ai_crawl` respects depth and page limits.
5. Redirected private/internal URLs are rejected.

## Migration and compatibility

Version 0.2.0 uses the current native plugin format: `plugin.yaml`, a namespaced
`register(ctx)` entry point, declared Python dependencies, and `config_schema`
for the Desktop settings form. It uses the documented web provider interface;
portable `plugin.json` packages do not replace native Python provider plugins.

Settings now use `ctx.get_config()` and `plugins.entries.hermes-web-crawl4ai.settings`.
The old `config` subtree still works through Hermes' migration fallback. When
both contain the same key, `settings` wins. Settings are read when used rather
than cached across profiles. Backend ID `crawl4ai`, tool `crawl4ai_crawl`,
`CRAWL4AI_API_TOKEN`, and existing URL/limit behavior are preserved.

Validated on Python 3.11 and 3.14 on 2026-11-06 against Hermes main commit
`6b21d0564de0492e2ebd0d8f63fca86c03b5c892`: unit/HTTP contract tests, real directory
and pip discovery, Plugin Doctor, and catalog validation. The two plugins were
also loaded together in an isolated profile. Live-service tests remain opt-in;
they were not rerun for this update. The historical 0.8.9 live result above is
not a claim of current-server compatibility.

Catalog validation passes with a private-import warning for Hermes' URL
normalization, async URL safety, and website-policy helpers. These existing
checks are retained so the plugin's crawl tool also respects host policy; there
is currently no documented plugin API for the complete website-policy check.
CI tests against current Hermes to detect changes to those imports.

Reference: [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
and [web provider contract](https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin).

## Distribution policy

This repository is the distribution unit. It will not be proposed for the
Hermes core tree or any plugin catalog until live compatibility and security
testing are complete.

## License

MIT
