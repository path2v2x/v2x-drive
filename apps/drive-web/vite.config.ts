import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Local development talks to the production host for everything served by
// path-rfs, so config.json can stay root-relative in every environment.
// Override with DRIVE_PROXY_TARGET=https://<host> (e.g. a local nginx).
const proxyTarget = process.env.DRIVE_PROXY_TARGET ?? 'https://drive.path2v2x.net';
const proxiedPrefixes = ['/ws', '/camera', '/archive', '/detections', '/data'];

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		port: 5173,
		host: true,
		proxy: Object.fromEntries(
			proxiedPrefixes.map((prefix) => [
				prefix,
				{ target: proxyTarget, changeOrigin: true, ws: prefix === '/ws' }
			])
		)
	}
});
