/** @type {import('next').NextConfig} */
const nextConfig = {
  // Required for containerised production builds (Docker / Kubernetes).
  // Produces .next/standalone with an inline node server; static assets are
  // copied separately in the Dockerfile runner stage.
  output: "standalone",
  images: {
    remotePatterns: [
      {
        protocol: "http",
        hostname: "**",
      },
      {
        protocol: "https",
        hostname: "**",
      },
    ],
  },
};

export default nextConfig;
