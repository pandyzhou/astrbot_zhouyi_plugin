import { resolve } from 'node:path';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  return {
    base: './',
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 35021,
      allowedHosts: true,
      proxy: {
        '/api/plug/astrbot_zhouyi_plugin': {
          target: env.VITE_API_PROXY_TARGET || 'https://127.0.0.1:35015',
          changeOrigin: true,
          secure: false,
          rewrite: (path) => path.replace(
            /^\/api\/plug\/astrbot_zhouyi_plugin/,
            '/api/v1/plugins/extensions/astrbot_zhouyi_plugin',
          ),
        },
      },
    },
    build: {
      outDir: resolve(__dirname, '../../../pages/zhouyi-dashboard'),
      emptyOutDir: true,
    },
  };
});
