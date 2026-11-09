FROM nousresearch/hermes-agent:latest
# v0.23+ dropped uv from the image
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
RUN uv pip install --python /opt/hermes/.venv/bin/python3 \
    "psycopg[binary]==3.2.*" \
    "sentence-transformers" \
    "tplinkrouterc6u"
