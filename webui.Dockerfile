FROM ghcr.io/nesquena/hermes-webui:latest
RUN uv pip install --python /opt/hermes/.venv/bin/python3 \
    "psycopg[binary]==3.2.*" \
    "sentence-transformers"
