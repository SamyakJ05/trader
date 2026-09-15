/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone bundles only what the server needs to run, so the production
  // image carries neither the source tree nor the full node_modules.
  output: "standalone",
  // The app sits behind a reverse proxy holding TLS; this stops Next
  // advertising a version that tells an attacker what to target.
  poweredByHeader: false,
  // Two lockfiles exist -- the workspace root's and this package's -- because
  // the Docker build context is this directory and cannot reach the root one.
  // Left to infer, Next picks whichever it finds first, which differs between
  // a local build and the image build. Pin it to this package, which is what
  // both actually build.
  outputFileTracingRoot: import.meta.dirname,
};

export default nextConfig;
