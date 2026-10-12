import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchDemoVideos } from '$lib/api';
import { resetRuntimeConfigCache } from '$lib/runtime-config';

function jsonResponse(body: unknown, ok = true): Response {
	return { ok, status: ok ? 200 : 500, json: async () => body } as Response;
}

const listing = [
	{ name: 'Street_Camera_Detections.mp4', type: 'file', mtime: 'Wed, 22 Apr 2026 00:29:09 GMT', size: 8196159 },
	{ name: 'Street_Camera_Detections.jpg', type: 'file', mtime: 'Mon, 12 Oct 2026 22:30:00 GMT', size: 1000 },
	{ name: 'ERV_V2X_with_Captions.mp4', type: 'file', mtime: 'Mon, 12 Oct 2026 22:25:37 GMT', size: 39223704 },
	{ name: 'notes.txt', type: 'file', mtime: 'Mon, 12 Oct 2026 22:25:37 GMT', size: 10 },
	{ name: 'old', type: 'directory', mtime: 'Mon, 12 Oct 2026 22:25:37 GMT' }
];

beforeEach(() => {
	resetRuntimeConfigCache();
});

afterEach(() => {
	vi.unstubAllGlobals();
	resetRuntimeConfigCache();
});

describe('demo videos', () => {
	it('lists videos newest first and pairs each with a same-stem poster', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn((url: string) =>
				Promise.resolve(url.startsWith('/config.json') ? jsonResponse({ dataBaseUrl: '/data' }) : jsonResponse(listing))
			)
		);

		const videos = await fetchDemoVideos();

		expect(videos.map((video) => video.fileName)).toEqual(['ERV_V2X_with_Captions.mp4', 'Street_Camera_Detections.mp4']);
		expect(videos[0]).toMatchObject({ title: 'ERV V2X with Captions', posterUrl: null });
		expect(videos[1]).toMatchObject({
			title: 'Street Camera Detections',
			url: '/data/demo-videos/Street_Camera_Detections.mp4',
			posterUrl: '/data/demo-videos/Street_Camera_Detections.jpg'
		});
	});
});
