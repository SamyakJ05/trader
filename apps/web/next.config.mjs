import path from "node:path";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone bundles only what the server needs to run, so the production
  // image carries neither the source tree nor the full node_modules.
  output: "standalone",
  // The app sits behind a reverse proxy holding TLS; this stops Next
  // advertising a version that tells an attacker what to target.
  poweredByHeader: false,
  // Pin to the pnpm workspace root (one level up), not this package: the
  // Docker build now uses the repo root as its context so it can reach the
  // one real lockfile there, and standalone's output layout depends on
  // where tracing believes the workspace root is. Left to infer, Next
  // guesses from the nearest lockfile, which differs between a local build
  // (finds it immediately) and the image build (finds it one level up) if
  // this isn't pinned explicitly.
  outputFileTracingRoot: path.join(import.meta.dirname, ".."),
};

export default nextConfig;
