FROM oven/bun:1.4.2-slim@sha256:cb3bbbb08e13a4a2ff400f24c7a2a1d5efa83f6ef8544d52d95a519631e2fc61

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
