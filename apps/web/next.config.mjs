/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone bundles only what the server needs to run, so the production
  // image carries neither the source tree nor the full node_modules.
  output: "standalone",
  // The app sits behind a reverse proxy holding TLS; this stops Next
  // advertising a version that tells an attacker what to target.
  poweredByHeader: false,
};

export default nextConfig;
