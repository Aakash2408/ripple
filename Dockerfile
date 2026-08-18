FROM python:3.11-slim

WORKDIR /app

# --- TypeScript toolchain -----------------------------------------------------
# Without this the image has no node, no npm and no docker daemon, so
# choose_backend() returns "" and EVERY generated fix is UNABLE_TO_VALIDATE --
# correctly not a pass, which meant no cell could reach AUTO in production however
# honestly the registry derived it. Measured, not assumed:
#
#     docker run --rm python:3.11-slim -> node ABSENT npm ABSENT docker ABSENT
#     choose_backend() -> ''   validate() -> UNABLE_TO_VALIDATE
#
# Copied from the official node image rather than apt-installed so the version is
# pinned and reproducible; `apt-get install nodejs` would drift with the Debian
# release and make a verdict unreproducible, which is the same reason
# DOCKER_IMAGE is a pinned tag.
#
# NOTE: adding this changes NOTHING on its own. The host validation backend is a
# DEGRADED path (no network isolation, no cgroups) and app/validation.py refuses to
# select it unless RIPPLE_ALLOW_DEGRADED_VALIDATION=1 is set explicitly. The
# toolchain being present and the risk being accepted are two separate decisions,
# and conflating them is how a safety fallback becomes the default nobody chose.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# Use shell form so $PORT gets expanded at runtime
CMD uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8000}
