FROM node:24-bookworm-slim

ENV NODE_ENV=production \
    PYTHON=python3 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        python3 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

COPY --chown=node:node src/ ./src/
RUN mkdir -p .wwebjs_auth .wwebjs_cache \
    && chown node:node .wwebjs_auth .wwebjs_cache

USER node

CMD ["node", "src/whatsapp/bot.js"]
