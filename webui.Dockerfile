FROM ghcr.io/nesquena/hermes-webui:latest

# hermes-webui has no baked-in agent — it stages the hermes-agent source it
# finds mounted at runtime (see docker-compose.yaml's hermes-agent-src volume)
# and `uv pip install -e`s it into a *fresh* venv at /app/venv on every
# container start. That venv only gets hermes-agent's own pyproject.toml
# deps, so behavior-logger's extra deps (psycopg, sentence-transformers)
# never show up there no matter what we install into this image itself
# (upstream issue #681: tool calls run in this container/venv, not the
# `hermes` gateway's). Patch the one-time-per-fresh-venv install step in the
# entrypoint to also install them, right where it installs hermes-agent's own
# deps and marks .deps_installed so it isn't repeated on every restart.
RUN sed -i -e 's/touch \/app\/venv\/\.deps_installed/uv pip install "psycopg[binary]==3.2.*" "sentence-transformers" --trusted-host pypi.org --trusted-host files.pythonhosted.org || error_exit "Failed to install behavior-logger plugin dependencies"; touch \/app\/venv\/\.deps_installed/' /hermeswebui_init.bash
