# ── Stage: deps ──────────────────────────────────────────────────────────────
# Install node_modules only; cached unless package-lock.json changes.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --frozen-lockfile \
 && npm install @next/swc-linux-x64-musl@14.2.5 --save-optional 2>/dev/null || true

# ── Stage: dev ───────────────────────────────────────────────────────────────
# Used by docker-compose for local development.
# Source is volume-mounted at runtime; node_modules come from the deps stage.
FROM node:20-alpine AS dev
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
ENV NEXT_TELEMETRY_DISABLED=1
EXPOSE 3000
CMD ["npx", "next", "dev", "--port", "3000"]

# ── Stage: builder ────────────────────────────────────────────────────────────
# Produces a Next.js standalone build for production.
# Pass NEXT_PUBLIC_API_BASE_URL as a build arg if the frontend and API are on
# separate origins; leave blank to rely on same-origin ingress routing.
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE_URL=""
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ── Stage: runner (production) ────────────────────────────────────────────────
# Minimal image — only the standalone output and static assets are copied.
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
 && adduser  --system --uid 1001 nextjs

COPY --from=builder /app/public                          ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static     ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

CMD ["node", "server.js"]
