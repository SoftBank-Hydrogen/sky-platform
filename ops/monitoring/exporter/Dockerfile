FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
WORKDIR /observer
COPY package*.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY observer.cjs discovery.cjs ./
USER node
EXPOSE 9108
CMD ["node", "observer.cjs"]
