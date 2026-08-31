/** @type {import('next').NextConfig} */
const isProd = process.env.NODE_ENV === 'production';
const isDocker = process.env.DOCKER_BUILD === 'true';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:17177';

const nextConfig = {
    output: isProd ? 'export' : undefined,
    distDir: isProd ? (isDocker ? 'out' : '../static') : undefined,
    basePath: isProd && !isDocker ? '/static' : undefined,
    assetPrefix: isProd && !isDocker ? '/static' : undefined,
    // Dev-only: proxy /api-proxy/* to backend to avoid CORS issues (e.g. file downloads).
    // Static exports must not declare rewrites, even when the returned list is empty.
    ...(!isProd && {
        async rewrites() {
            return [
                {
                    source: '/api-proxy/:path*',
                    destination: `${BACKEND_URL}/:path*`,
                },
            ];
        },
    }),
    eslint: {
        ignoreDuringBuilds: true,
    },
    typescript: {
        ignoreBuildErrors: true,
    },
    images: {
        unoptimized: true,
        remotePatterns: [
            {
                protocol: "https",
                hostname: "placehold.co",
            },
            {
                protocol: "https",
                hostname: "yino-drama.oss-cn-beijing.aliyuncs.com",
                pathname: "/assets/**",
            },
            {
                protocol: "http",
                hostname: "localhost",
                port: "17177",
            },
        ],
    },
};

export default nextConfig;
