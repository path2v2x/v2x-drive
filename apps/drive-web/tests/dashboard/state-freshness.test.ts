import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import type { StateJson } from '$lib/api';
import type { TrackedObject } from '$lib/types';
import { bridgeStatus, objects } from '$lib/stores/objects';
import {
	applyStateSnapshot,
	resetStateSnapshotFreshness,
	wsConnected
} from '$lib/stores/websocket';

const NOW = Date.parse('2026-07-10T00:00:30Z');

function tracked(objectId: string): TrackedObject {
	return {
		object_id: objectId,
		object_type: 'vehicle',
		lat: 37.9,
		lon: -122.3,
		confidence: 0.9,
		street_name: 'Test',
		timestamp_utc: '2026-07-10T00:00:00Z',
		snapshot_url: null,
		snapshot_timestamp: null,
		last_updated: NOW
	};
}

function snapshot(updatedAt: string | null, objectId: string): StateJson {
	return {
		objects: [tracked(objectId)],
		bridge_status: {
			status: 'connected',
			carla_fps: 20,
			objects_tracked: 1,
			cameras_active: 4,
			last_heartbeat: updatedAt
		},
		updated_at: updatedAt
	};
}

beforeEach(() => {
	resetStateSnapshotFreshness();
	objects.set(new Map());
	wsConnected.set(false);
});

describe('state snapshot freshness', () => {
	it('applies a producer-fresh snapshot and preserves its producer time', () => {
		expect(applyStateSnapshot(snapshot('2026-07-10T00:00:20Z', 'fresh'), NOW)).toBe(true);
		expect([...get(objects).keys()]).toEqual(['fresh']);
		expect(get(wsConnected)).toBe(true);
		expect(get(bridgeStatus).updated_at).toBe('2026-07-10T00:00:20.000Z');
	});

	it('rejects a stale snapshot without replacing current objects', () => {
		applyStateSnapshot(snapshot('2026-07-10T00:00:20Z', 'fresh'), NOW);
		expect(applyStateSnapshot(snapshot('2026-07-09T23:59:00Z', 'stale'), NOW)).toBe(false);
		expect([...get(objects).keys()]).toEqual(['fresh']);
		expect(get(bridgeStatus).status).toBe('stale');
		expect(get(wsConnected)).toBe(false);
	});

	it('rejects an out-of-order snapshot even while both snapshots are fresh', () => {
		applyStateSnapshot(snapshot('2026-07-10T00:00:25Z', 'newer'), NOW);
		expect(applyStateSnapshot(snapshot('2026-07-10T00:00:20Z', 'older'), NOW)).toBe(false);
		expect([...get(objects).keys()]).toEqual(['newer']);
	});

	it('accepts numeric-string producer heartbeat timestamps', () => {
		const state = snapshot(null, 'numeric');
		state.bridge_status!.last_heartbeat = String(NOW / 1000 - 1);
		expect(applyStateSnapshot(state, NOW)).toBe(true);
		expect([...get(objects).keys()]).toEqual(['numeric']);
	});
});
