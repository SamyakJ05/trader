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

# Confirmed by inspecting the built image directly (outputFileTracingRoot's
# exact nesting behaviour isn't obvious from docs alone): standalone output
# lands at .next/standalone/web/server.js -- nested one level, by package
# name, not by its full apps/web path. Copy the whole standalone tree, then
# run from within that one level of nesting.
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./web/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/public ./web/public

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/web

CMD ["node", "server.js"]
