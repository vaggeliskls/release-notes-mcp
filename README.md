# release-notes-mcp

<!-- mcp-name: io.github.vaggeliskls/release-notes-mcp -->

A small, generic MCP server that combines GitHub releases from several
repositories into a single product release note. The server just fetches and
bundles raw data; the LLM synthesizes the final notes.

Nothing is architecture-specific:

- **`provider`** — which forge to read releases from: `github` (default),
  `gitlab`, or `gitea`/Forgejo. Release fetching goes through a small adapter,
  so adding a forge means normalizing its release JSON — a contained change.
- **`repos`** — the repos the server is allowed to read releases from.
- **`contextSources`** — arbitrary URLs loaded as background context (a style
  guide, a versions file, feature names — anything). The server assigns no
  meaning; what each source *is* is decided by what you put behind the URL.

## Configuration

Config holds **no secrets** — only the repo set and context. Provider and auth
come from the environment.

```jsonc
// config.json — non-sensitive (required; the server errors if it's missing)
{
  "repos": [
    "myorg/auth-service",
    "myorg/web"
  ],
  "contextSources": [
    {
      "name": "release-info",
      "url": "https://example.github.io/whatever/release.json",
      "description": "Extra context to consult when assembling release notes"
    }
  ]
}
```

Environment (provider-agnostic, set in `.env` or your shell):

| Var | Purpose | Default |
|-----|---------|---------|
| `TOKEN` | Auth token for the provider — **never in config** | _(empty; ok for public repos)_ |
| `PROVIDER` | `github` \| `gitlab` \| `gitea` (overrides config) | `github` |
| `BASE_URL` | API base — only for self-hosted GitLab / Gitea | provider default |

- `format` on a context source is **optional** — auto-detected from
  `Content-Type` / URL extension / content sniffing. Override only when wrong.

**The config (repos + contextSources) must come from one of two places** — the
server errors on startup if neither is set:

| Source | Use it for |
|--------|-----------|
| `RELEASE_MCP_CONFIG_JSON` | The config as **inline JSON**. No file needed — ideal for `uvx` / MCP hubs where everything is an env var. |
| `RELEASE_MCP_CONFIG` | Path to a `config.json` **file** (default `./config.json`). Used by the container, which mounts a real file. |

Inline JSON wins when both are set. Copy `config.example.json` to get started
with the file approach.

## Tools

| Tool | Purpose |
|------|---------|
| `list_repos()` | The configured repos |
| `list_releases(repo, limit)` | Recent releases for one repo |
| `get_latest_version(repo)` | Newest release for one repo |
| `get_release(repo, tag)` | Full notes for one tag |
| `compare_releases(repo, from_tag, to_tag)` | All releases between two versions |
| `gather_release_notes(selections[])` | Bundle raw notes from N `(repo, tag)` pairs (concurrent) |
| `get_context(name?)` | Load configured context URLs (auto-detected format) |

Selection is **dynamic** — you (or Claude) pass the `(repo, tag)` pairs to
combine. The server's `instructions` tell Claude to call `get_context()` first.

## Run

The server runs in a container over **HTTP transport** on `localhost:8000`.
First create the config and env files (both runs need them):

```bash
cp config.example.json config.json   # edit repos + contextSources (no secrets)
cp .env.example .env                  # set TOKEN (+ PROVIDER / BASE_URL if needed)
```

### Normal run

```bash
docker compose up -d
```

### Local development — `docker compose watch`

For local dev, `docker compose watch` keeps the server live while you edit:

```bash
docker compose watch
```

| Change | Action |
|--------|--------|
| `server.py` | **sync + restart** — copied into the container, process restarts |
| `requirements.txt`, `Dockerfile` | **rebuild** — image is rebuilt automatically |
| `config.json` | bind-mounted (live); run `docker compose restart` to reload it |

### Run with `uvx` (no clone, no container)

The server is published to PyPI, so a client can launch it on demand with
[`uvx`](https://docs.astral.sh/uv/) — no checkout and no Docker:

```bash
uvx release-notes-mcp
```

`uvx` talks to the server over **stdio** (the default transport). Since there's
no file to mount, pass the config **inline** as JSON via `RELEASE_MCP_CONFIG_JSON`
(everything is env-only — ideal for MCP hubs):

```bash
RELEASE_MCP_CONFIG_JSON='{"repos":["myorg/web"],"contextSources":[]}' \
  TOKEN=ghp_... uvx release-notes-mcp
```

Prefer a file? Point `RELEASE_MCP_CONFIG` at an **absolute** path instead
(`uvx` runs from an unknown working directory, so a relative path won't resolve):

```bash
RELEASE_MCP_CONFIG=/abs/path/config.json TOKEN=ghp_... uvx release-notes-mcp
```

## Register with Claude Code

**HTTP (container)** — point Claude Code at the running server by its URL:

```bash
claude mcp add --transport http release-notes http://localhost:8000/mcp
```

**stdio (`uvx`)** — let Claude Code launch the server as a subprocess:

```bash
claude mcp add release-notes \
  --env RELEASE_MCP_CONFIG=/abs/path/config.json \
  --env TOKEN=ghp_... \
  -- uvx release-notes-mcp
```

Then ask Claude: *"Combine the latest releases of auth-service and web into a
product release note."*
