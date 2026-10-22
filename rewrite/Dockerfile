FROM oven/bun:1.4.2-slim

WORKDIR /app
COPY --chown=bun:bun package.json bun.lock ./
# Use shipped prebuilds or JS fallbacks, without optional native build scripts.
RUN bun install --frozen-lockfile --production --ignore-scripts \
    && mkdir -p /app/data \
    && chown bun:bun /app/data
COPY --chown=bun:bun src ./src

ENV NODE_ENV=production \
    MODE=shadow \
    DB_PATH=/app/data/misaka.sqlite \
    JOURNAL_PATH=/app/data/events.jsonl \
    SPAM_CONFIG_PATH=/app/config.yaml
VOLUME ["/app/data"]
USER bun
STOPSIGNAL SIGTERM
CMD ["bun", "run", "src/main.ts"]
