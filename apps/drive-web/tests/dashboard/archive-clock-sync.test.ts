import { describe, expect, it } from 'vitest';

import {
	ARCHIVE_MAX_CURSOR_DRIFT_MS,
	archiveCursorNeedsCorrection,
	archiveMediaTimeForEpoch,
	coverageBucketSecondsForSpan,
	layoutHistogram,
	layoutMarkers
} from '../../src/lib/timeline';

describe('archive clock synchronisation', () => {
	it('maps wall time to clip media time and clamps before the clip start', () => {
		expect(archiveMediaTimeForEpoch(10_750, 10_000)).toBe(0.75);
		expect(archiveMediaTimeForEpoch(9_000, 10_000)).toBe(0);
	});

	it('corrects drift only when it exceeds the strict replay tolerance', () => {
		expect(archiveCursorNeedsCorrection(10_000, 10_000 + ARCHIVE_MAX_CURSOR_DRIFT_MS)).toBe(
			false
		);
		expect(
			archiveCursorNeedsCorrection(10_000, 10_001 + ARCHIVE_MAX_CURSOR_DRIFT_MS)
		).toBe(true);
		expect(
			archiveCursorNeedsCorrection(10_000, 9_999 - ARCHIVE_MAX_CURSOR_DRIFT_MS)
		).toBe(true);
	});

	it('never seeks from invalid clock values', () => {
		expect(archiveCursorNeedsCorrection(Number.NaN, 10_000)).toBe(false);
		expect(archiveCursorNeedsCorrection(10_000, Number.POSITIVE_INFINITY)).toBe(false);
	});
});

describe('timeline layout', () => {
	const start = Date.parse('2026-09-03T16:00:00.000Z');
	const end = Date.parse('2026-09-03T17:00:00.000Z');

	it('places object markers at first sighting, coloured by type', () => {
		const markers = layoutMarkers(
			[
				{
					object_id: 'person_cam-001-ch2_1',
					object_type: 'person',
					first_seen: '2026-09-03T16:30:00.000Z',
					last_seen: '2026-09-03T16:30:15.000Z',
					count: 129,
					max_confidence: 0.93,
					cameras: ['ch2'],
					last_lat: 37.9158,
					last_lon: -122.3347
				},
				{
					object_id: 'car_out_of_view',
					object_type: 'car',
					first_seen: '2026-09-03T17:30:00.000Z',
					last_seen: '2026-09-03T17:30:15.000Z',
					count: 1,
					max_confidence: 0.5,
					cameras: ['ch1'],
					last_lat: 37.9158,
					last_lon: -122.3347
				}
			],
			start,
			end
		);
		expect(markers).toHaveLength(1);
		expect(markers[0].x).toBeCloseTo(0.5);
		expect(markers[0].color).toBe('#4ade80');
	});

	it('scales coverage bars relative to the busiest visible bucket', () => {
		const bars = layoutHistogram(
			[
				{ start: '2026-09-03T16:00:00.000Z', detections: 5, objects: 1 },
				{ start: '2026-09-03T16:05:00.000Z', detections: 0, objects: 0 },
				{ start: '2026-09-03T16:10:00.000Z', detections: 10, objects: 2 }
			],
			300,
			start,
			end
		);
		expect(bars.map((bar) => bar.intensity)).toEqual([0.5, 0, 1]);
		expect(bars[2].x).toBeCloseTo(10 / 60);
	});

	it('picks coverage buckets that keep the request under the server limit', () => {
		expect(coverageBucketSecondsForSpan(10 * 60 * 1000)).toBe(10);
		expect(coverageBucketSecondsForSpan(24 * 60 * 60 * 1000)).toBe(120);
		expect(coverageBucketSecondsForSpan(72 * 60 * 60 * 1000)).toBe(360);
	});
});
