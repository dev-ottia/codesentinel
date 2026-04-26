/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    // Inside Docker: frontend container must reach backend via service name.
    // INTERNAL_API_URL is set in docker-compose (not NEXT_PUBLIC_ — it's
    // server-side only and never exposed to the browser).
    // Outside Docker (local npm run dev): falls back to localhost:8000.
    const apiUrl =
      process.env.INTERNAL_API_URL ||
      process.env.NEXT_PUBLIC_API_URL ||
      'http://localhost:8000';

    return [
      {
        source: '/api/backend/:path*',
        destination: `${apiUrl}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
