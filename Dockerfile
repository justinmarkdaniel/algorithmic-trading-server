# syntax=docker/dockerfile:1.4
###############################################################################
# algorithmic-trading-server — multi-stage Docker build
#
# Defaults to linux/arm64 (cheap Graviton or Ampere instances); override with
# `--platform linux/amd64` for x86 hosts. The image is split into a heavy
# builder (compiles TA-Lib + wheels, slow but cached) and a lean runtime
# (~300 MB, no compilers, no source build artefacts).
###############################################################################
ARG TARGETPLATFORM=linux/arm64

###############################################################################
# Stage 1: builder — compile wheels + system deps in an isolated layer
###############################################################################
FROM --platform=${TARGETPLATFORM} python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    wget \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# TA-Lib C library — required by pandas-ta for the ~60 CDL_* candle pattern
# indicators. Without it, any strategy referencing a CDL_* column gets a
# silently-zeroed series and never fires.
RUN cd /tmp \
 && wget -q https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz \
 && tar xf ta-lib-0.6.4-src.tar.gz && cd ta-lib-0.6.4 \
 && ./configure --prefix=/usr/local --build="$(uname -m)-unknown-linux-gnu" \
 && make -j"$(nproc)" && make install \
 && cd / && rm -rf /tmp/ta-lib* \
 && ldconfig

WORKDIR /build

# === Layer A: dependency wheels (cached on pyproject.toml hash) ===
# Stub the source so `pip wheel .` can resolve the project without touching
# the real source tree. Every dependency wheel is built and cached here, so
# day-to-day code changes never reinvalidate this expensive layer (cold
# rebuild ~25-40 min on QEMU emulation; warm cache ~30 s).
COPY pyproject.toml ./
RUN mkdir -p server \
 && touch server/__init__.py \
 && pip install --upgrade pip setuptools wheel \
 && pip wheel --wheel-dir /wheels . \
 && rm -f /wheels/algorithmic_trading_server-*.whl /wheels/algorithmic-trading-server-*.whl \
 && rm -rf server

# === Layer B: project wheel — cheap rebuild on every commit ===
COPY server ./server
RUN pip wheel --wheel-dir /wheels --no-deps .

###############################################################################
# Stage 2: runtime — slim image with only what we need
###############################################################################
FROM --platform=${TARGETPLATFORM} python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    BOT_LOG_DIR=/app/logs \
    BOT_CONFIG_PATH=/app/server/config/production.json

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/libta_lib* /usr/local/lib/
COPY --from=builder /usr/local/include/ta-lib /usr/local/include/ta-lib
RUN ldconfig

WORKDIR /app

# Install from wheels via a BuildKit bind-mount instead of COPY+rm. COPY+rm
# leaves the wheel artefacts in the COPY layer (image layers are immutable),
# bloating the runtime image by hundreds of MB. The bind-mount makes /wheels
# available only during this RUN — wheels never get baked into the runtime
# image.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels,readonly \
    pip install --no-index --find-links /wheels algorithmic-trading-server

COPY server ./server
COPY infra/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /app/logs

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS -o /dev/null "http://127.0.0.1:8000/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
