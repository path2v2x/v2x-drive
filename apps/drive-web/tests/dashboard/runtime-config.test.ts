import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadRuntimeConfig, resetRuntimeConfigCache, resolveWsUrl } from '$lib/runtime-config';

function jsonResponse(body: unknown, ok = true): Response {
	return { ok, json: async () => body } as Response;
}

const staticConfig = {
	wsUrl: '/ws',
	dataBaseUrl: '/data/',
	detectionsBaseUrl: 'https://twin.example.test/detections/',
	archiveVideoBaseUrl: 'https://drive.example.test/archive/'
};

beforeEach(() => {
	window.history.replaceState({}, '', '/');
	resetRuntimeConfigCache();
});

afterEach(() => {
	vi.unstubAllGlobals();
	resetRuntimeConfigCache();
});

describe('runtime config', () => {
	it('loads endpoints from config.json and trims trailing slashes', async () => {
		const fetchMock = vi.fn().mockResolvedValue(jsonResponse(staticConfig));
		vi.stubGlobal('fetch', fetchMock);

		const config = await loadRuntimeConfig();

		expect(config.wsUrl).toBe('/ws');
		expect(config.dataBaseUrl).toBe('/data');
		expect(config.detectionsBaseUrl).toBe('https://twin.example.test/detections');
		expect(config.archiveVideoBaseUrl).toBe('https://drive.example.test/archive');
		expect(fetchMock).toHaveBeenCalledWith(
			expect.stringMatching(/^\/config\.json\?v=\d+$/),
			{ cache: 'no-store' }
		);
	});

	it('falls back to same-origin defaults when config.json is unavailable', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

		const config = await loadRuntimeConfig();

		expect(config.wsUrl).toBe('/ws');
		expect(config.detectionsBaseUrl).toBe('/detections');
		expect(config.liveVideoUrlTemplate).toBe('');
	});

	it('reloads config.json after the cache is reset', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(jsonResponse(staticConfig))
			.mockResolvedValueOnce(jsonResponse({ ...staticConfig, wsUrl: 'wss://replacement.example.test/ws' }));
		vi.stubGlobal('fetch', fetchMock);

		await loadRuntimeConfig();
		resetRuntimeConfigCache();
		const config = await loadRuntimeConfig();

		expect(config.wsUrl).toBe('wss://replacement.example.test/ws');
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});
});

describe('resolveWsUrl', () => {
	it('resolves root-relative paths against the page origin with a matching scheme', () => {
		expect(resolveWsUrl('/ws', { protocol: 'https:', host: 'path2v2x.net' })).toBe('wss://path2v2x.net/ws');
		expect(resolveWsUrl('/ws', { protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws');
	});

	it('keeps absolute WebSocket URLs and rewrites http schemes', () => {
		expect(resolveWsUrl('wss://drive.example.test/ws/', undefined)).toBe('wss://drive.example.test/ws');
		expect(resolveWsUrl('http://127.0.0.1:8765', undefined)).toBe('ws://127.0.0.1:8765');
		expect(resolveWsUrl('', undefined)).toBe('');
	});
});
