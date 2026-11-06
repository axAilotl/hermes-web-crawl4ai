"""Start a disposable official service, run real HTTP tests, and remove it.

Requires Docker and this repository's development/test dependencies.
No existing services, configuration, or credentials are used.
"""

import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE = "crawl4ai"
IMAGE = "unclecode/crawl4ai@sha256:bd36741e7bdd35ddc1a05d9183e1d6d8cefb61dd640d944a25d026b76e917690"
PORT = 11235
ROOT = Path(__file__).resolve().parents[1]


def main():
    name = f"hermes-plugin-test-{SERVICE}-{secrets.token_hex(5)}"
    with tempfile.TemporaryDirectory(prefix="hermes-plugin-live-") as temporary:
        work = Path(temporary)
        env = os.environ.copy()
        for key in ("CRAWL4AI_API_TOKEN", "SEARXNG_API_TOKEN", "CRAWL4AI_URL", "SEARXNG_URL"):
            env.pop(key, None)
        env["HERMES_HOME"] = str(work / "hermes")
        command = ["docker", "create", "--rm", "--name", name, "-p", f"127.0.0.1::{PORT}"]
        if SERVICE == "crawl4ai":
            env["CRAWL4AI_API_TOKEN"] = secrets.token_hex(32)
            command += ["--shm-size=1g", "-e", "CRAWL4AI_API_TOKEN"]
        else:
            config = work / "config"
            config.mkdir()
            (config / "settings.yml").write_text(
                "use_default_settings: true\nserver:\n  secret_key: "
                + secrets.token_hex(32)
                + "\n  limiter: false\n  image_proxy: false\nsearch:\n  formats: [html, json]\n",
                encoding="utf-8",
            )

        try:
            subprocess.run([*command, IMAGE], env=env, check=True, capture_output=True, text=True)
            if SERVICE == "searxng":
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        str(config / "settings.yml"),
                        f"{name}:/etc/searxng/settings.yml",
                    ],
                    check=True,
                    capture_output=True,
                )
            subprocess.run(["docker", "start", name], check=True, capture_output=True)
            mapping = (
                subprocess.check_output(["docker", "port", name, str(PORT)], text=True)
                .strip()
                .splitlines()[0]
            )
            endpoint = "http://" + mapping
            health = "/health" if SERVICE == "crawl4ai" else "/healthz"
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urllib.request.urlopen(endpoint + health, timeout=2) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"{SERVICE} did not become healthy") from None
                time.sleep(1)
            env[f"{SERVICE.upper()}_TEST_URL"] = endpoint
            return subprocess.run(
                [sys.executable, "-m", "pytest", "-q", f"tests/test_live_{SERVICE}.py"],
                cwd=ROOT,
                env=env,
                check=False,
            ).returncode
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
