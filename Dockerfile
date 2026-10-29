# --- build: resolve deps into wheels (git is only needed here, for the TrueNAS client) ---
FROM python:3.13-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# --- runtime: small, non-root, no compilers or git ---
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin nasbot \
 && mkdir -p /data && chown nasbot /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && pip check && rm -rf /wheels
USER nasbot
VOLUME ["/data"]
# Fails if the monitor loop hasn't completed a cycle in max(3 x poll interval, 3 min).
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["nasbot", "healthcheck"]
ENTRYPOINT ["nasbot"]
CMD ["run"]
