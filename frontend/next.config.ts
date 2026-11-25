import type { NextConfig } from "next";

// The browser talks only to Next.js; /api/* is proxied to the Python FastAPI backend.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` }];
  },
  // No floating dev badge over the UI (it covered text in demo screenshots).
  devIndicators: false,
  experimental: {
    // A local model can take well over the default 30 s for one agent turn.
    proxyTimeout: 300_000,
  },
};

export default nextConfig;
