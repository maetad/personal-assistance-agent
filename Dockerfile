FROM nousresearch/hermes-agent:latest
RUN uv pip install --python /opt/hermes/.venv/bin/python3 \
    "psycopg[binary]==3.2.*" \
    "sentence-transformers" \
    "tplinkrouterc6u"
