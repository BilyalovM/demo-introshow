FROM node:20-bookworm-slim

# Chromium deps for whatsapp-web.js / Puppeteer
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
  && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    SESSION_PATH=/data/session \
    PORT=3001 \
    WA_BRIDGE_AUTOSTART=1

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.js ./

RUN mkdir -p /data/session
VOLUME ["/data/session"]
EXPOSE 3001

CMD ["npm", "start"]
