FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY postgres-migrator.js ./
COPY rds-global-bundle.pem /app/rds-global-bundle.pem
ENV NODE_EXTRA_CA_CERTS=/app/rds-global-bundle.pem
COPY migrations ./migrations
CMD ["node", "postgres-migrator.js"]
