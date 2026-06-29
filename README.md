# release-notes-mcp

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
- The config path defaults to `./config.json`; override with `RELEASE_MCP_CONFIG`.

`config.json` is **required** — the server raises an error on startup if it's
missing. Copy `config.example.json` to get started.

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

## Run with `docker compose watch`

The server runs in a container over **HTTP transport** on `localhost:8000`.
`docker compose watch` keeps it live while you edit:

```bash
cp config.example.json config.json   # edit repos + contextSources (no secrets)
cp .env.example .env                  # set TOKEN (+ PROVIDER / BASE_URL if needed)
docker compose watch
```

| Change | Action |
|--------|--------|
| `server.py` | **sync + restart** — copied into the container, process restarts |
| `requirements.txt`, `Dockerfile` | **rebuild** — image is rebuilt automatically |
| `config.json` | bind-mounted (live); run `docker compose restart` to reload it |

## Register with Claude Code

Point Claude Code at the running HTTP server by its URL:

```bash
claude mcp add --transport http release-notes http://localhost:8000/mcp
```

Then ask Claude: *"Combine the latest releases of auth-service and web into a
product release note."*
