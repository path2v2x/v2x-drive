import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TrackedObject } from '../../src/lib/types';
import { objects } from '../../src/lib/stores/objects';


function tracked(overrides: Partial<TrackedObject> = {}): TrackedObject {
	return {
		object_id: 'object-1',
		object_type: 'vehicle',
		lat: 37.9,
		lon: -122.3,
		confidence: 0.9,
		street_name: 'test',
		timestamp_utc: '2026-07-10T01:00:00.000Z',
		snapshot_url: null,
		snapshot_timestamp: null,
		last_updated: 1234,
		...overrides
	};
}


describe('object producer time', () => {
	beforeEach(() => objects.set(new Map()));
	afterEach(() => vi.restoreAllMocks());

	it('preserves producer last_updated rather than browser receipt time', () => {
		vi.spyOn(Date, 'now').mockReturnValue(9_999_999_999_999);
		objects.upsert(tracked({ last_updated: 1234 }));
		expect(get(objects).get('object-1')?.last_updated).toBe(1234);
	});

	it('derives missing producer time from timestamp_utc without using Date.now', () => {
		vi.spyOn(Date, 'now').mockReturnValue(9_999_999_999_999);
		objects.upsert(tracked({ last_updated: Number.NaN }));
		expect(get(objects).get('object-1')?.last_updated).toBe(
			Date.parse('2026-07-10T01:00:00.000Z')
		);
	});

	it('keeps the metadata clock unchanged for a snapshot update', () => {
		objects.upsert(tracked());
		objects.updateSnapshot(
			'object-1',
			'https://example.test/snapshot.jpg',
			'2026-07-10T01:01:00.000Z'
		);
		expect(get(objects).get('object-1')?.last_updated).toBe(1234);
	});

	it('setAll applies producer, type, and street-only changes', () => {
		objects.setAll([
			tracked({
				last_updated: Date.parse('2026-07-10T01:00:00.000Z')
			})
		]);
		objects.setAll([
			tracked({
				object_type: 'traffic_cone',
				street_name: 'new street',
				timestamp_utc: '2026-07-10T01:00:05.000Z',
				last_updated: Date.parse('2026-07-10T01:00:05.000Z')
			})
		]);

		const updated = get(objects).get('object-1');
		expect(updated?.object_type).toBe('traffic_cone');
		expect(updated?.street_name).toBe('new street');
		expect(updated?.timestamp_utc).toBe('2026-07-10T01:00:05.000Z');
		expect(updated?.last_updated).toBe(Date.parse('2026-07-10T01:00:05.000Z'));
	});

	it('setAll rejects an out-of-order object without removing it', () => {
		objects.setAll([
			tracked({
				street_name: 'newest',
				timestamp_utc: '2026-07-10T01:00:10.000Z',
				last_updated: Date.parse('2026-07-10T01:00:10.000Z')
			})
		]);
		objects.setAll([
			tracked({
				street_name: 'stale overwrite',
				timestamp_utc: '2026-07-10T01:00:01.000Z',
				last_updated: Date.parse('2026-07-10T01:00:01.000Z')
			})
		]);

		expect(get(objects).get('object-1')?.street_name).toBe('newest');
	});

	it('rejects an older snapshot update', () => {
		objects.upsert(
			tracked({
				snapshot_url: 'https://example.test/new.jpg',
				snapshot_timestamp: '2026-07-10T01:02:00.000Z',
				last_updated: Date.parse('2026-07-10T01:02:00.000Z')
			})
		);
		objects.updateSnapshot(
			'object-1',
			'https://example.test/old.jpg',
			'2026-07-10T01:01:00.000Z'
		);

		expect(get(objects).get('object-1')?.snapshot_url).toBe('https://example.test/new.jpg');
	});

	it('accepts newer metadata without rolling back a newer snapshot', () => {
		objects.upsert(
			tracked({
				street_name: 'old metadata',
				timestamp_utc: '2026-07-10T01:00:00.000Z',
				last_updated: Date.parse('2026-07-10T01:00:00.000Z'),
				snapshot_url: 'https://example.test/new-snapshot.jpg',
				snapshot_timestamp: '2026-07-10T01:03:00.000Z'
			})
		);

		objects.upsert(
			tracked({
				street_name: 'new metadata',
				timestamp_utc: '2026-07-10T01:02:00.000Z',
				last_updated: Date.parse('2026-07-10T01:02:00.000Z'),
				snapshot_url: 'https://example.test/old-snapshot.jpg',
				snapshot_timestamp: '2026-07-10T01:01:00.000Z'
			})
		);

		const updated = get(objects).get('object-1');
		expect(updated?.street_name).toBe('new metadata');
		expect(updated?.last_updated).toBe(Date.parse('2026-07-10T01:02:00.000Z'));
		expect(updated?.snapshot_url).toBe('https://example.test/new-snapshot.jpg');
		expect(updated?.snapshot_timestamp).toBe('2026-07-10T01:03:00.000Z');
	});

	it('accepts a newer snapshot without rolling back newer metadata', () => {
		objects.setAll([
			tracked({
				street_name: 'new metadata',
				timestamp_utc: '2026-07-10T01:03:00.000Z',
				last_updated: Date.parse('2026-07-10T01:03:00.000Z'),
				snapshot_url: 'https://example.test/old-snapshot.jpg',
				snapshot_timestamp: '2026-07-10T01:01:00.000Z'
			})
		]);

		objects.setAll([
			tracked({
				street_name: 'old metadata',
				timestamp_utc: '2026-07-10T01:02:00.000Z',
				last_updated: Date.parse('2026-07-10T01:02:00.000Z'),
				snapshot_url: 'https://example.test/new-snapshot.jpg',
				snapshot_timestamp: '2026-07-10T01:04:00.000Z'
			})
		]);

		const updated = get(objects).get('object-1');
		expect(updated?.street_name).toBe('new metadata');
		expect(updated?.last_updated).toBe(Date.parse('2026-07-10T01:03:00.000Z'));
		expect(updated?.snapshot_url).toBe('https://example.test/new-snapshot.jpg');
		expect(updated?.snapshot_timestamp).toBe('2026-07-10T01:04:00.000Z');
	});

	it('allows a newer snapshot clock to explicitly clear an image', () => {
		objects.upsert(
			tracked({
				snapshot_url: 'https://example.test/image.jpg',
				snapshot_timestamp: '2026-07-10T01:01:00.000Z'
			})
		);
		objects.upsert(
			tracked({
				snapshot_url: null,
				snapshot_timestamp: '2026-07-10T01:02:00.000Z'
			})
		);

		expect(get(objects).get('object-1')?.snapshot_url).toBeNull();
		expect(get(objects).get('object-1')?.snapshot_timestamp).toBe(
			'2026-07-10T01:02:00.000Z'
		);
	});
});
