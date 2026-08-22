# Flowify — one image, two roles, selected by $FLOWIFY_MODE at runtime:
#   FLOWIFY_MODE=server  Hugging Face Spaces / any hosted deploy. Git-URL
#                        ingest only (see backend/app/cloner.py), /shutdown
#                        disabled, graphs scoped to X-Flowify-Session.
#   FLOWIFY_MODE=local   `docker run -v <your-code>:/repos` on your own
#                        machine. Local paths under /repos, /shutdown on,
#                        one visitor (you), no scoping.
#
# Stage 1 builds the static frontend; stage 2 is the Python backend that
# also serves that build (see the StaticFiles mount at the bottom of
# backend/app/main.py). One process, one port — no nginx/proxy needed.

FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim

# Skip .pyc writes (nothing benefits from them in a container that starts
# once and doesn't restart the interpreter) and flush stdout/stderr
# immediately so Render's log stream isn't waiting on a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git is a hard runtime dependency, not just a build tool: cloner.py shells
# out to it for git-URL ingest, and git_updater.py uses it for /update.
# One RUN so the apt lists removal actually shrinks this layer instead of
# just the next one.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 user

COPY backend/requirements.txt /tmp/requirements.txt
# Plain uvicorn, not uvicorn[standard]: the extras (uvloop, httptools,
# websockets, watchfiles, python-dotenv, pyyaml) are for perf tuning,
# websocket routes, --reload, and .env loading — this app has none of
# those (single worker by necessity, see the CMD comment below; no
# websocket endpoints; env vars come from Render/docker run directly).
RUN pip install -r /tmp/requirements.txt

WORKDIR /app/backend
COPY backend/ /app/backend/
COPY --from=web /web/dist /app/frontend_dist

ENV FLOWIFY_FRONTEND_DIST=/app/frontend_dist \
    FLOWIFY_STORE=/home/user/store \
    FLOWIFY_WORKDIR=/tmp/flowify \
    FLOWIFY_MODE=server \
    FLOWIFY_ALLOWED_ROOTS=/repos \
    LLM_PROVIDER=heuristic \
    PORT=7860

RUN mkdir -p /home/user/store /tmp/flowify /repos && chown -R user:user /home/user /tmp/flowify /repos
USER user

EXPOSE 7860

# --workers 1 is required, not a default: storage.py is SQLite in WAL mode
# with thread-local connections and a save() that deletes-and-reinserts every
# node/edge per graph — that's single-writer-per-process, not multi-worker
# safe. Scale this app by giving it more CPU, not more uvicorn workers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
