import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchState } = vi.hoisted(() => ({ fetchState: vi.fn() }));
vi.mock('$lib/api', () => ({ fetchState }));

import { bridgeStatus, objects } from '$lib/stores/objects';
import {
	connectWebSocket,
	disconnectWebSocket,
	wsConnected
} from '$lib/stores/websocket';
import type { StateJson } from '$lib/api';

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((resolvePromise) => {
		resolve = resolvePromise;
	});
	return { promise, resolve };
}

beforeEach(() => {
	disconnectWebSocket();
	objects.set(new Map());
	fetchState.mockReset();
});

afterEach(() => {
	disconnectWebSocket();
	vi.restoreAllMocks();
});

describe('state polling disconnect', () => {
	it('does not apply a response that finishes after disconnect', async () => {
		vi.spyOn(console, 'log').mockImplementation(() => undefined);
		const request = deferred<StateJson>();
		fetchState.mockReturnValueOnce(request.promise);
		connectWebSocket();
		expect(fetchState).toHaveBeenCalledTimes(1);

		disconnectWebSocket();
		request.resolve({
			updated_at: new Date().toISOString(),
			bridge_status: {
				status: 'connected',
				last_heartbeat: new Date().toISOString(),
				objects_tracked: 1
			},
			objects: [
				{
					object_id: 'ghost',
					object_type: 'vehicle',
					lat: 37.9,
					lon: -122.3,
					confidence: 0.9,
					street_name: 'test',
					timestamp_utc: new Date().toISOString(),
					snapshot_url: null,
					snapshot_timestamp: null,
					last_updated: Date.now()
				}
			]
		});
		await Promise.resolve();
		await Promise.resolve();

		expect(get(objects).has('ghost')).toBe(false);
		expect(get(wsConnected)).toBe(false);
		expect(get(bridgeStatus).status).toBe('disconnected');
	});
});
