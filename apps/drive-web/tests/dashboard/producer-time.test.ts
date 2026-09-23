import { describe, expect, it } from 'vitest';
import {
	isProducerTimestampFresh,
	latestProducerTimestamp,
	normalizeProducerTimestamp,
	producerEpochMs
} from '$lib/producer-time';

describe('producer timestamps', () => {
	it('normalizes ISO, numeric seconds, and numeric-string seconds', () => {
		expect(normalizeProducerTimestamp('2026-07-10T00:00:00Z')).toBe(
			'2026-07-10T00:00:00.000Z'
		);
		expect(producerEpochMs(1_000)).toBe(1_000_000);
		expect(producerEpochMs('1000')).toBe(1_000_000);
	});

	it('rejects missing, malformed, stale, and far-future timestamps', () => {
		const now = Date.parse('2026-07-10T00:00:30Z');
		expect(isProducerTimestampFresh(null, 30_000, now)).toBe(false);
		expect(isProducerTimestampFresh('not-a-time', 30_000, now)).toBe(false);
		expect(isProducerTimestampFresh('2026-07-09T23:59:59Z', 30_000, now)).toBe(false);
		expect(isProducerTimestampFresh('2026-07-10T00:00:40Z', 30_000, now)).toBe(false);
	});

	it('chooses the latest timestamp chronologically rather than lexicographically', () => {
		expect(
			latestProducerTimestamp([
				'2026-07-09T17:00:01-07:00',
				'2026-07-10T00:00:02Z',
				'invalid'
			])
		).toBe('2026-07-10T00:00:02.000Z');
	});
});
