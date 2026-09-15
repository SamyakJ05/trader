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

# Confirmed by inspecting the built image directly: standalone output lands
# at .next/standalone/web/server.js -- nested one level, by package name.
# Its node_modules/next etc are pnpm symlinks with a *relative* target
# (../../../node_modules/.pnpm/...) baked in at the depth they existed at in
# the builder stage: apps/web/node_modules/, three levels below the real
# pnpm store the deps stage installed at repo-root node_modules/.pnpm.
# Copying only .next/standalone reproduces the symlinks but not what they
# point at, and putting them at any other depth breaks the relative path
# outright -- which is what happened when this first landed at
# /app/web/node_modules (only two levels under /app): require('next')
# resolved to /node_modules/.pnpm/..., outside /app entirely.
#
# Fix: reproduce the builder's exact depth. /app/apps/web/web/... puts
# node_modules three levels under /app, same as the builder had, so the
# pre-existing relative symlinks resolve on their own -- and the real pnpm
# store is copied to the same relative target they already point at.
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./apps/web/
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/public ./apps/web/web/public
COPY --from=builder --chown=nextjs:nodejs /app/node_modules/.pnpm ./node_modules/.pnpm

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/apps/web/web

CMD ["node", "server.js"]
