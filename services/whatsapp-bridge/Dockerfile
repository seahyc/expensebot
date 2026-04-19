FROM node:20-alpine

# Install build tools needed for native modules (better-sqlite3)
RUN apk add --no-cache python3 make g++

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

# Auth state and DB live in mounted volumes
RUN mkdir -p /app/auth /app/db

ENV AUTH_DIR=/app/auth
ENV DB_PATH=/app/db/messages.db
ENV PORT=3001

EXPOSE 3001

CMD ["node", "index.js"]
