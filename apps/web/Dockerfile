# Production image for the Next.js app. Build context is the REPO ROOT, not
# apps/web: the pnpm workspace root (and its lockfile, the one npm actually
# resolves against) lives one level up, and a context scoped to apps/web
# would only ever see the stale per-package copy.
FROM node:20-alpine AS deps
WORKDIR /app
RUN corepack enable
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile

FROM node:20-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/web/node_modules ./apps/web/node_modules
COPY apps/web ./apps/web
WORKDIR /app/apps/web
# The API URL is baked in at build time by Next, so it must be supplied here
# rather than at run time.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN pnpm build

FROM node:20-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app

RUN addgroup -g 10001 -S nodejs && adduser -u 10001 -S nextjs -G nodejs

# outputFileTracingRoot is pinned to the workspace root (one level up from
# apps/web, in next.config.mjs), so standalone's output mirrors that same
# nesting: server.js lands at .next/standalone/apps/web/server.js, not at
# the top level. Copy the whole standalone tree, then run from within it.
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/public ./apps/web/public

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/apps/web

CMD ["node", "server.js"]
