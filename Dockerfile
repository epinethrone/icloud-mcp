FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data MCP_HOST=0.0.0.0 BRIDGE_HOST=0.0.0.0 MCP_PORT=8000
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["python", "-m", "icloud_mcp"]
