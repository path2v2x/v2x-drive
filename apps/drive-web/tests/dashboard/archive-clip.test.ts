import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/runtime-config', () => ({
	buildAssetUrl: vi.fn(),
	loadRuntimeConfig: vi.fn(async () => ({
		archiveVideoBaseUrl: 'https://drive.example.test/archive/'
	}))
}));

import { archiveClipUrl, archiveListUrl, listArchiveSegments } from '$lib/api';
import { archiveEpochForMediaTime } from '$lib/timeline';

beforeEach(() => {
	vi.stubGlobal(
		'fetch',
		vi.fn(async () => ({
			ok: true,
			json: async () => [
				{ start: '2026-09-02T17:00:10Z', duration: 9.5 },
				{ start: 'invalid', duration: 20 },
				{ start: '2026-09-02T17:00:00Z', duration: 10 }
			]
		}))
	);
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('MediaMTX archive clips', () => {
	it('builds an encoded segment-list URL with both window bounds', () => {
		expect(
			archiveListUrl(
				'https://drive.example.test/archive/',
				'ch 1',
				'2026-09-02T17:00:00.000Z',
				'2026-09-02T17:15:00.000Z'
			)
		).toBe(
			'https://drive.example.test/archive/list?path=ch+1&start=2026-09-02T17%3A00%3A00.000Z&end=2026-09-02T17%3A15%3A00.000Z'
		);
	});

	it('builds a standard MP4 clip URL', () => {
		expect(
			archiveClipUrl(
				'https://drive.example.test/archive',
				'ch1',
				'2026-09-02T17:00:00.000Z',
				42.5
			)
		).toBe(
			'https://drive.example.test/archive/get?path=ch1&start=2026-09-02T17%3A00%3A00.000Z&duration=42.5&format=mp4'
		);
	});

	it('lists, validates, and orders recorded segments', async () => {
		const segments = await listArchiveSegments(
			'ch1',
			'2026-09-02T17:00:00.000Z',
			'2026-09-02T17:15:00.000Z'
		);

		expect(segments).toEqual([
			{ start: '2026-09-02T17:00:00Z', duration: 10 },
			{ start: '2026-09-02T17:00:10Z', duration: 9.5 }
		]);
		expect(fetch).toHaveBeenCalledWith(
			'https://drive.example.test/archive/list?path=ch1&start=2026-09-02T17%3A00%3A00.000Z&end=2026-09-02T17%3A15%3A00.000Z',
			{ cache: 'no-store' }
		);
	});

	it('treats MediaMTX no-segments responses as an empty archive window', async () => {
		vi.mocked(fetch).mockResolvedValueOnce({
			ok: false,
			status: 404
		} as Response);

		await expect(
			listArchiveSegments(
				'ch1',
				'2026-08-28T17:00:00.000Z',
				'2026-08-28T17:15:00.000Z'
			)
		).resolves.toEqual([]);
	});

	it('maps native MP4 media time from the exact clip start', () => {
		const startMs = Date.parse('2026-09-02T17:00:03.250Z');
		expect(archiveEpochForMediaTime(startMs, 12.75)).toBe(
			Date.parse('2026-09-02T17:00:16.000Z')
		);
	});
});
