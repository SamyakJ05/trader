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

# Standalone output lands at .next/standalone/web/server.js -- nested one
# level, by package name. Its node_modules/next etc are pnpm symlinks with a
# fixed *relative* target, ../../../node_modules/.pnpm/..., baked in at the
# depth node_modules existed at in the builder stage: apps/web/node_modules/
# -- exactly three path segments under /app (apps, web, node_modules) -- so
# ../../../ from inside it lands squarely on /app, where the deps stage
# installed the real pnpm store.
#
# Traced this with actual `cd` hops on the built image rather than counting
# by eye a second time, because eye-counting got it wrong twice already:
# copying standalone's contents AS a subdirectory (.../apps/web/web/...)
# adds a fourth segment, and ../../../ then lands one level short, at
# /app/apps instead of /app -- a break `find` doesn't reveal, since it does
# not dereference symlinks by default, but `ls`/`require` do.
#
# Fix: copy standalone's *inner* web/ directory's contents directly onto
# apps/web/, not standalone/ itself as a new subdirectory -- collapsing that
# extra nesting level back to exactly three segments, matching the builder.
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone/web ./apps/web/
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/public ./apps/web/public
COPY --from=builder --chown=nextjs:nodejs /app/node_modules/.pnpm ./node_modules/.pnpm

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/apps/web

CMD ["node", "server.js"]
