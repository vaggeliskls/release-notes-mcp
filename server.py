"""
release-mcp — a small, generic MCP server for combining releases from several
repositories into product release notes.

Design:
  * `repos`          — the set of repos this server is allowed to read.
  * `contextSources` — arbitrary URLs loaded as background context.
  * Tools fetch / compare / bundle releases; Claude synthesizes the notes.

The forge (github | gitlab | gitea), base URL, and auth token come from the
environment (`PROVIDER` / `BASE_URL` / `TOKEN`), never from config.json.

Release fetching goes through a small Provider adapter, so adding a forge is a
contained change (normalize its release JSON into the common shape). Nothing
here is specific to any one architecture or to any single forge.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class Provider:
    """
    Base adapter. A provider knows how to fetch releases for one repo and how to
    normalize a raw release into the common shape:

        {tag, name, published_at, prerelease, url, body}

    `repo` is always 'owner/name' (GitLab: 'group/project', nesting allowed).
    """

    name = "base"
    default_base = ""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base = (base_url or self.default_base).rstrip("/")
        self.token = token

    def headers(self) -> dict[str, str]:
        return {}

    def normalize(self, r: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def list_releases(self, c: httpx.AsyncClient, repo: str, limit: int) -> list[dict]:
        raise NotImplementedError

    async def get_latest(self, c: httpx.AsyncClient, repo: str) -> dict:
        rs = await self.list_releases(c, repo, 1)
        if not rs:
            raise ValueError(f"No releases found for '{repo}'")
        return rs[0]

    async def get_by_tag(self, c: httpx.AsyncClient, repo: str, tag: str) -> dict:
        raise NotImplementedError


class GitHubProvider(Provider):
    name = "github"
    default_base = "https://api.github.com"

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def normalize(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": r.get("tag_name"),
            "name": r.get("name"),
            "published_at": r.get("published_at"),
            "prerelease": r.get("prerelease"),
            "url": r.get("html_url"),
            "body": r.get("body") or "",
        }

    async def list_releases(self, c, repo, limit):
        r = await c.get(
            f"{self.base}/repos/{repo}/releases",
            params={"per_page": limit},
            headers=self.headers(),
        )
        r.raise_for_status()
        return [self.normalize(x) for x in r.json()]

    async def get_latest(self, c, repo):
        r = await c.get(f"{self.base}/repos/{repo}/releases/latest", headers=self.headers())
        r.raise_for_status()
        return self.normalize(r.json())

    async def get_by_tag(self, c, repo, tag):
        r = await c.get(f"{self.base}/repos/{repo}/releases/tags/{tag}", headers=self.headers())
        r.raise_for_status()
        return self.normalize(r.json())


class GitLabProvider(Provider):
    name = "gitlab"
    default_base = "https://gitlab.com/api/v4"

    def _pid(self, repo: str) -> str:
        # GitLab addresses projects by URL-encoded full path (group/sub/project).
        return quote(repo, safe="")

    def headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def normalize(self, r: dict[str, Any]) -> dict[str, Any]:
        links = r.get("_links") or {}
        return {
            "tag": r.get("tag_name"),
            "name": r.get("name"),
            "published_at": r.get("released_at"),
            "prerelease": r.get("upcoming_release"),
            "url": links.get("self"),
            "body": r.get("description") or "",
        }

    async def list_releases(self, c, repo, limit):
        r = await c.get(
            f"{self.base}/projects/{self._pid(repo)}/releases",
            params={"per_page": limit, "order_by": "released_at", "sort": "desc"},
            headers=self.headers(),
        )
        r.raise_for_status()
        return [self.normalize(x) for x in r.json()]

    async def get_by_tag(self, c, repo, tag):
        r = await c.get(
            f"{self.base}/projects/{self._pid(repo)}/releases/{quote(tag, safe='')}",
            headers=self.headers(),
        )
        r.raise_for_status()
        return self.normalize(r.json())


class GiteaProvider(Provider):
    """Gitea / Forgejo. Release shape is close to GitHub. Set `baseUrl`."""

    name = "gitea"
    default_base = ""  # self-hosted — must be configured, e.g. https://git.example.com/api/v1

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"token {self.token}"
        return h

    def normalize(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": r.get("tag_name"),
            "name": r.get("name"),
            "published_at": r.get("published_at"),
            "prerelease": r.get("prerelease"),
            "url": r.get("html_url") or r.get("url"),
            "body": r.get("body") or "",
        }

    async def list_releases(self, c, repo, limit):
        r = await c.get(
            f"{self.base}/repos/{repo}/releases",
            params={"limit": limit},
            headers=self.headers(),
        )
        r.raise_for_status()
        return [self.normalize(x) for x in r.json()]

    async def get_by_tag(self, c, repo, tag):
        r = await c.get(f"{self.base}/repos/{repo}/releases/tags/{tag}", headers=self.headers())
        r.raise_for_status()
        return self.normalize(r.json())


PROVIDERS = {p.name: p for p in (GitHubProvider, GitLabProvider, GiteaProvider)}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config() -> dict[str, Any]:
    """
    Load the non-secret config (repos + contextSources) from, in order:

      1. `RELEASE_MCP_CONFIG_JSON` — the config as inline JSON. Best for `uvx`
         and MCP hubs, where everything is passed as environment variables and
         there is no file to mount.
      2. The file at `RELEASE_MCP_CONFIG` (default `./config.json`) — used by the
         container, which bind-mounts a real config.

    One of the two must be set; otherwise the server has nothing to read.
    """
    inline = os.environ.get("RELEASE_MCP_CONFIG_JSON")
    if inline:
        cfg = json.loads(inline)
    else:
        path = Path(os.environ.get("RELEASE_MCP_CONFIG", "config.json"))
        if not path.exists():
            raise FileNotFoundError(
                f"No config found. Set RELEASE_MCP_CONFIG_JSON to inline JSON, or "
                f"point RELEASE_MCP_CONFIG at a config file (looked for: {path}). "
                f"Copy config.example.json to get started."
            )
        cfg = json.loads(path.read_text())
    cfg.setdefault("repos", [])
    cfg.setdefault("contextSources", [])
    return cfg


CONFIG = load_config()
REPOS: list[str] = CONFIG["repos"]
CONTEXT_SOURCES: list[dict[str, Any]] = CONFIG["contextSources"]

# Provider / base URL / token all come from the environment. They are never
# stored in config.json, which holds only the (non-secret) repo set and context.
_provider_name = (os.environ.get("PROVIDER") or "github").lower()
_base_url = os.environ.get("BASE_URL") or ""
_token = os.environ.get("TOKEN", "")

if _provider_name not in PROVIDERS:
    raise ValueError(f"Unknown provider '{_provider_name}'. Choose from: {', '.join(PROVIDERS)}")
PROVIDER: Provider = PROVIDERS[_provider_name](_base_url, _token)
if not PROVIDER.base:
    raise ValueError(
        f"Provider '{_provider_name}' requires a base URL (set the BASE_URL env var)."
    )


def check_repo(repo: str) -> None:
    """Keep the server scoped to configured repos."""
    if REPOS and repo not in REPOS:
        raise ValueError(
            f"Repo '{repo}' is not in the configured scope. "
            f"Allowed: {', '.join(REPOS) or '(none configured)'}"
        )


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

INSTRUCTIONS = """
This server combines releases from several repositories into product release
notes.

Recommended flow when asked to assemble release notes:
  1. Call `get_context()` first to load any supplementary context the user has
     configured (style guides, feature names, version info, anything).
  2. Use `list_repos`, `list_releases`, `get_latest_version`, `compare_releases`
     to find the relevant releases.
  3. Call `gather_release_notes(selections=[...])` to bundle the raw notes.
  4. Synthesize a single product release note. Choose the best structure for the
     content (by component, by change type, or a mix) and dedupe across repos.
""".strip()

mcp = FastMCP("release-notes", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Tools — repos & releases
# --------------------------------------------------------------------------- #


@mcp.tool()
def list_repos() -> dict[str, Any]:
    """List the repositories and provider this server is configured to read."""
    return {"provider": PROVIDER.name, "repos": REPOS}


@mcp.tool()
async def list_releases(repo: str, limit: int = 10) -> list[dict[str, Any]]:
    """List recent releases for one repo (newest first). `repo` is 'owner/name'."""
    check_repo(repo)
    async with httpx.AsyncClient(timeout=15) as c:
        return await PROVIDER.list_releases(c, repo, limit)


@mcp.tool()
async def get_latest_version(repo: str) -> dict[str, Any]:
    """Get the latest published release for one repo."""
    check_repo(repo)
    async with httpx.AsyncClient(timeout=15) as c:
        return await PROVIDER.get_latest(c, repo)


@mcp.tool()
async def get_release(repo: str, tag: str) -> dict[str, Any]:
    """Get the full release notes for a specific tag in one repo."""
    check_repo(repo)
    async with httpx.AsyncClient(timeout=15) as c:
        return await PROVIDER.get_by_tag(c, repo, tag)


@mcp.tool()
async def compare_releases(repo: str, from_tag: str, to_tag: str) -> list[dict[str, Any]]:
    """
    Return every release in `repo` published after `from_tag` up to and including
    `to_tag` (newest first) — useful when a service jumped several versions.
    """
    check_repo(repo)
    async with httpx.AsyncClient(timeout=15) as c:
        releases = await PROVIDER.list_releases(c, repo, 100)

    tags = [x.get("tag") for x in releases]
    if to_tag not in tags:
        raise ValueError(f"to_tag '{to_tag}' not found in {repo}")
    to_idx = tags.index(to_tag)
    from_idx = tags.index(from_tag) if from_tag in tags else len(tags)
    return releases[to_idx:from_idx]


@mcp.tool()
async def gather_release_notes(selections: list[dict[str, str]]) -> list[dict[str, Any]]:
    """
    Bundle raw release notes for an explicit list of selections so they can be
    synthesized into a single product release.

    `selections` is a list of {"repo": "owner/name", "tag": "v1.2.3"}.
    Fetches all entries concurrently.
    """
    for s in selections:
        check_repo(s["repo"])

    async with httpx.AsyncClient(timeout=15) as c:

        async def one(sel: dict[str, str]) -> dict[str, Any]:
            rel = await PROVIDER.get_by_tag(c, sel["repo"], sel["tag"])
            return {"repo": sel["repo"], **rel}

        return await asyncio.gather(*(one(s) for s in selections))


# --------------------------------------------------------------------------- #
# Tools — context
# --------------------------------------------------------------------------- #


def _detect_and_parse(resp: httpx.Response, declared: str | None) -> Any:
    """Auto-detect format (override with `declared`) and parse accordingly."""
    fmt = declared
    if not fmt:
        ctype = resp.headers.get("content-type", "").lower()
        url = str(resp.url).lower()
        if "json" in ctype or url.endswith(".json"):
            fmt = "json"
        elif "yaml" in ctype or url.endswith((".yaml", ".yml")):
            fmt = "yaml"
        else:
            fmt = "text"

    if fmt == "json":
        try:
            return resp.json()
        except Exception:
            return resp.text
    return resp.text


@mcp.tool()
async def get_context(name: str = "") -> list[dict[str, Any]]:
    """
    Load supplementary context the user configured in `contextSources`.

    Call this first when assembling release notes. With no argument it loads all
    sources; pass `name` to load just one. Format is auto-detected.
    """
    sources = CONTEXT_SOURCES
    if name:
        sources = [s for s in CONTEXT_SOURCES if s.get("name") == name]
        if not sources:
            raise ValueError(f"No context source named '{name}'")
    if not sources:
        return []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:

        async def one(src: dict[str, Any]) -> dict[str, Any]:
            resp = await c.get(src["url"])
            resp.raise_for_status()
            return {
                "name": src.get("name", src["url"]),
                "url": src["url"],
                "description": src.get("description", ""),
                "content": _detect_and_parse(resp, src.get("format")),
            }

        return await asyncio.gather(*(one(s) for s in sources))


def main() -> None:
    """Console-script entry point (`release-notes-mcp`, also used by `uvx`).

    stdio (default) for a client-launched subprocess; http to run as a service.
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http", "sse"):
        mcp.run(
            transport=transport,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()

