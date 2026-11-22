# Self-hosted DataDoctor: run it next to your DHIS2 so health data never leaves your network.
#   docker build -t datadoctor .
#   docker run --rm -e DHIS2_URL=... -e DHIS2_USER=... -e DHIS2_PASSWORD=... \
#     -v "$PWD/config:/app/config" -v "$PWD/reports:/app/reports" datadoctor --config config/my-country.toml report
#   Web app:  docker run --rm -p 8000:8000 -e DATADOCTOR_PASSWORD=... -e DHIS2_URL=... ... datadoctor serve --host 0.0.0.0
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY config ./config
RUN useradd --create-home datadoctor && mkdir -p reports && chown datadoctor reports
USER datadoctor
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/datadoctor"]
CMD ["report"]
