/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",

  async rewrites() {
    return [
      {
        source: "/api/proxy/:path*",
        destination:
          "https://parker-joint-proven-providing.trycloudflare.com/api/:path*",
      },
    ];
  },
};

export default nextConfig;
