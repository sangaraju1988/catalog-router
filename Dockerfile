# syntax=docker/dockerfile:1

# ---- build stage: build a wheel with all dependencies --------------------------
FROM python:3.13-slim AS build
WORKDIR /src
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

# ---- runtime stage: no compiler, no source tree, non-root ---------------------
FROM python:3.13-slim AS runtime

ARG VERSION=0.1.1
LABEL org.opencontainers.image.title="catalog-router" \
      org.opencontainers.image.description="MCP server that routes BI questions to dbt, Looker, Tableau, Superset or BigQuery using a semantic catalog graph" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CATALOG_PATH=/catalog/catalog.yaml \
    MCP_TRANSPORT=stdio \
    PORT=8080

RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels catalog-router \
 && python -m pip uninstall -y pip setuptools wheel \
 && rm -rf /usr/local/lib/python3*/ensurepip/_bundled \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin router \
 && mkdir -m 0755 /catalog

# The bundled example catalog makes the image runnable out of the box.
# Mount your own over it:  -v ./my-catalog.yaml:/catalog/catalog.yaml:ro
# (/catalog is created above: COPY --chmod would otherwise apply 0444 to the
#  directory too, and the non-root user could not traverse it.)
COPY --chown=root:root --chmod=0444 catalog/example-catalog.yaml /catalog/catalog.yaml

USER 10001
EXPOSE 8080

# Fail fast at build time if the bundled catalog is invalid.
RUN catalog-router --check

ENTRYPOINT ["catalog-router"]
