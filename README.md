# personal-assistance-agent

Hermes Agent (Telegram) + Postgres/pgvector, with a custom `behavior-logger`
plugin that silently writes structured habit logs and semantic memories per
profile. Everything the stack needs to run is either in this repo or in two
data directories outside it.

## What's tracked here vs. what lives outside the repo

| In this repo (git) | Outside the repo (per machine, not versioned) |
|---|---|
| `docker-compose.yaml`, `Dockerfile`, `Makefile` | `~/.hermes` — all profiles, secrets, plugin *installs*, kanban/memory DBs |
| `plugins/behavior-logger/` — plugin source | `./data/hermes-db` — Postgres data directory |
| `.env.example` | `.env` — actual secrets (gitignored) |

`plugins/behavior-logger` in this repo is the source of truth for the
plugin code. `make install-plugin` copies it into a profile's
`~/.hermes/profiles/<name>/plugins/` directory and enables it — the copy
inside `~/.hermes` is a deployed artifact, not something to hand-edit.

## Option A: move the whole running instance to a new machine

Copies everything as-is (same profiles, same Telegram token, same DB rows).

```bash
rsync -a this-repo/        newmachine:personal-assistance-agent/   # excludes .env, /data via .gitignore-like rules if you use rsync --exclude
scp this-repo/.env         newmachine:personal-assistance-agent/.env
rsync -a ~/.hermes/        newmachine:~/.hermes/
rsync -a this-repo/data/   newmachine:personal-assistance-agent/data/
```

Then on the new machine:

```bash
cd personal-assistance-agent
make up   # docker compose build hermes && docker compose up -d
```

## Option B: fresh setup on a new machine (new profile, no old data)

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, HERMES_DASHBOARD_BASIC_AUTH_PASSWORD,
# and the OPENAI_* vars if your provider uses them.

make up                                          # builds the hermes image, starts db/hermes/adminer
make create-profile NAME=pan TOKEN=<telegram-bot-token>

# model provider isn't set via .env - it's per-profile:
docker exec -it hermes-agent hermes -p pan config set model.provider nous
docker exec -it hermes-agent hermes -p pan config set model.base_url https://inference-api.nousresearch.com/v1
docker exec -it hermes-agent hermes -p pan config set model.default upstage/solar-pro4:free
# plus whatever secret the provider needs, e.g.:
docker exec -it hermes-agent hermes -p pan config set NOUS_API_KEY <key>

make install-plugin NAME=pan                     # deploys plugins/behavior-logger and restarts pan
```

Verify:

```bash
docker exec hermes-db psql -U postgres -d hermes -c '\dt' -c '\dx'
docker exec -it hermes-agent hermes -p pan plugins doctor behavior-logger
```

`structured_logs` and `semantic_memories` are created automatically by the
plugin on first turn (idempotent, advisory-lock guarded) — no manual SQL
needed.

## Web UI (hermes-webui, one per profile)

[hermes-webui](https://github.com/nesquena/hermes-webui) gives a full
chat/session/workspace browser UI, separate from the ops-focused dashboard
on port 9119 (profile/gateway management, kept as-is). It isn't a REST
client — it imports Hermes Agent's Python package directly and needs the
same `HERMES_HOME` layout, and its only auth is one shared password per
running instance. So instead of one shared webui with a profile switcher,
there's **one `webui-<name>` container per profile**, each bind-mounted to
only that profile's directory (`./hermes-home/profiles/<name>`) and nothing
else. The container's filesystem view *is* the isolation boundary — a
profile's webui container can't see any other profile's data — backed up by
that profile's own password (`HERMES_WEBUI_PASSWORD_<NAME>` in `.env`).

To add webui access for a new profile, copy the `webui-pan` block in
`docker-compose.yaml`, rename it (`webui-<name>`, container name, volume
paths), pick the next free host port (8787 is taken by `pan`; use 8788,
8789, ...), and add `HERMES_WEBUI_PASSWORD_<NAME>` to `.env`.

```bash
docker compose up -d webui-pan
# → http://localhost:8787, log in with HERMES_WEBUI_PASSWORD_PAN
```

Known upstream limitation ([#681](https://github.com/nesquena/hermes-webui/issues/681)):
tool calls made from a webui chat session run inside the *webui* container,
using its own copy of Hermes Agent, not the `hermes` gateway container.
hermes-webui has no agent baked in — at every startup it stages whatever
agent source it finds mounted and `uv pip install`s it into a fresh venv,
so:

- `hermes` shares its `/opt/hermes` (agent source) with every `webui-<name>`
  container read-only via the `hermes-agent-src` named volume, so webui gets
  full functionality (model auto-detection, personality routing, CLI session
  imports) instead of degrading to a bare chat client. **After rebuilding the
  `hermes` image**, run `docker compose down && docker volume rm
  <project>_hermes-agent-src` before `up` — Docker only seeds this volume
  from the image on first creation, so a stale volume silently keeps the old
  agent source.
- `webui.Dockerfile` patches hermes-webui's entrypoint to also install
  `psycopg`/`sentence-transformers` into that fresh venv (its own
  `pyproject.toml` install only covers hermes-agent's own deps), so
  `behavior-logger` tool calls (e.g. `add_fact_key`) still work from the web
  chat, not just from Telegram/CLI.
- Each `webui-<name>` container's UID/GID is pinned via `WANTED_UID`/
  `WANTED_GID` (`10000`, matching `hermes`'s runtime user — check yours with
  `docker exec hermes-agent id`) so it can read/write its bind-mounted
  profile and state directories. Without this it falls back to its
  image-default `1024` and fails to start ("Permission denied" on its state
  dir).

## Notes

- The Postgres password and dashboard password now come from `.env` (see
  `.env.example`); `docker-compose.yaml` has no secrets in it, so it's safe
  to commit.
- `pgvector/pgvector:pg17` image; `DATABASE_URL` is a shared, non-secret
  connection string reused across all profiles (one DB, `profile_name`
  column per row) rather than one DB per profile.
