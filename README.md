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

## Notes

- The Postgres password and dashboard password now come from `.env` (see
  `.env.example`); `docker-compose.yaml` has no secrets in it, so it's safe
  to commit.
- `pgvector/pgvector:pg17` image; `DATABASE_URL` is a shared, non-secret
  connection string reused across all profiles (one DB, `profile_name`
  column per row) rather than one DB per profile.
