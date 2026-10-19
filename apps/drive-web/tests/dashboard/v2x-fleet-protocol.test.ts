import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { connect, disconnect, telemetry } from '$lib/stores/driveSocket';
import {
	EVENTS_MAX,
	despawnFleet,
	despawnFleetVehicle,
	fleetEvents,
	fleetStats,
	fleetTracks,
	fleetVehicles,
	requestFleet,
	resetFleetStore,
	setErvState,
	spawnCiviliansAhead,
	spawnErv,
	stateTone
} from '$lib/stores/v2xFleet';
import type { V2xFleetVehicle } from '$lib/types';

class MockWebSocket {
	static readonly CONNECTING = 0;
	static readonly OPEN = 1;
	static readonly CLOSING = 2;
	static readonly CLOSED = 3;
	static instances: MockWebSocket[] = [];

	readyState = MockWebSocket.CONNECTING;
	binaryType = '';
	sent: string[] = [];
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	onerror: ((event: Event) => void) | null = null;

	constructor(public readonly url: string) {
		MockWebSocket.instances.push(this);
	}

	open(): void {
		this.readyState = MockWebSocket.OPEN;
		this.onopen?.();
	}

	receive(message: object): void {
		this.onmessage?.({ data: JSON.stringify(message) });
	}

	send(payload: string): void {
		this.sent.push(payload);
	}

	close(): void {
		this.readyState = MockWebSocket.CLOSED;
		this.onclose?.();
	}

	sentJson(): Record<string, unknown>[] {
		return this.sent.map((s) => JSON.parse(s));
	}
}

function startSocket(): MockWebSocket {
	connect('wss://drive.example.test');
	const socket = MockWebSocket.instances.at(-1)!;
	socket.open();
	return socket;
}

function vehicle(id: number, kind: 'civilian' | 'erv', state = kind === 'erv' ? 'EN_ROUTE' : 'NORMAL'): V2xFleetVehicle {
	return {
		actor_id: id, name: `${kind === 'erv' ? 'drive_erv_' : 'drive_civ_'}${id}`, kind, profile: kind === 'erv' ? 'ambulance' : 'civilian',
		blueprint: 'vehicle.lincoln.mkz', state, reason: '', since_s: 0, pos: [0, 0], yaw: 0, speed_mps: 5, target_speed_mps: 10,
		offset_m: 0, stop: false, destination: [100, 0], dist_to_destination_m: 100, arrived: false, lane_change: null,
		passing: false, spawned_at: 1, ...(kind === 'erv' ? { siren: true, lights: true, response_type: 'emergency', manual: false, doors_open: false } : { lights: [], erv_key: null })
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetFleetStore();
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('V2X fleet protocol', () => {
	it('sends spawn requests and keeps the list from server replies and pushes', () => {
		const socket = startSocket();
		expect(requestFleet()).toBe(true);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'list_v2x_vehicles' });
		expect(spawnCiviliansAhead(3, 60, 25)).toBe(3);
		const spawns = socket.sentJson().filter((m) => m.type === 'spawn_v2x_vehicle');
		expect(spawns.map((m) => (m.spawn as { distance_m: number }).distance_m)).toEqual([60, 85, 110]);
		expect(spawns[0]).toMatchObject({ kind: 'civilian', profile: 'civilian', spawn: { ahead_of: 'ego', distance_m: 60 } });
		spawnErv('fire', 'behind', 120);
		expect(socket.sentJson().at(-1)).toEqual({
			type: 'spawn_v2x_vehicle', kind: 'erv', profile: 'fire', spawn: { behind: 'ego', distance_m: 120 }, destination: 'random'
		});
		spawnErv('police', 'random');
		expect(socket.sentJson().at(-1)).toMatchObject({ kind: 'erv', profile: 'police', spawn: 'random' });

		socket.receive({ type: 'v2x_vehicles', vehicles: [vehicle(1, 'civilian'), vehicle(2, 'erv')],
			tracks: [{ key: 'actor:2', active: true }], stats: { count: 2, max: 12, tick_ms_p95: 3.2, decision_hz: 10, degraded: false } });
		expect(get(fleetVehicles).map((v) => v.actor_id)).toEqual([1, 2]);
		expect(get(fleetTracks)).toHaveLength(1);
		expect(get(fleetStats)?.count).toBe(2);

		socket.receive({ type: 'v2x_vehicle_spawned', vehicle: vehicle(3, 'civilian'), count: 3 });
		expect(get(fleetVehicles)).toHaveLength(3);
		socket.receive({ type: 'v2x_vehicle_state', vehicle: { ...vehicle(2, 'erv', 'MANUAL'), siren: false, manual: true } });
		expect(get(fleetVehicles).find((v) => v.actor_id === 2)).toMatchObject({ state: 'MANUAL', siren: false, manual: true });
		socket.receive({ type: 'v2x_vehicle_despawned', actor_id: 3, count: 2 });
		expect(get(fleetVehicles).map((v) => v.actor_id)).toEqual([1, 2]);
		socket.receive({ type: 'v2x_vehicles_despawned', count: 2 });
		expect(get(fleetVehicles)).toEqual([]);
	});

	it('applies pushed events to the vehicle states and keeps a bounded log', () => {
		const socket = startSocket();
		socket.receive({ type: 'v2x_vehicles', vehicles: [vehicle(1, 'civilian')], tracks: [], stats: null });
		socket.receive({ type: 'v2x_vehicle_event', actor_id: 1, name: 'drive_civ_1', kind: 'civilian', from: 'NORMAL', to: 'MONITOR', reason: 'ERV behind', t: 10 });
		expect(get(fleetVehicles)[0]).toMatchObject({ state: 'MONITOR', reason: 'ERV behind' });
		expect(get(fleetEvents)).toHaveLength(1);
		for (let i = 0; i < EVENTS_MAX + 20; i++) {
			socket.receive({ type: 'v2x_vehicle_event', actor_id: 1, name: 'drive_civ_1', kind: 'civilian', from: 'MONITOR', to: 'MONITOR', reason: `tick ${i}`, t: 11 + i });
		}
		expect(get(fleetEvents)).toHaveLength(EVENTS_MAX);
		socket.receive({ type: 'v2x_vehicle_event', actor_id: 1, name: 'drive_civ_1', kind: 'civilian', from: 'MONITOR', to: null, reason: 'destroyed', t: 200 });
		expect(get(fleetVehicles)).toEqual([]);
	});

	it('updates positions from telemetry, sends operator commands and resets on disconnect', () => {
		const socket = startSocket();
		socket.receive({ type: 'v2x_vehicles', vehicles: [vehicle(2, 'erv')], tracks: [], stats: null });
		telemetry.set({ speed: 0, gear: 0, pos: [0, 0, 0], rot: [0, 0, 0], steer: 0, throttle: 0, brake: 0,
			v2x_fleet: [{ actor_id: 2, kind: 'erv', name: 'drive_erv_2', state: 'APPROACH_JUNCTION', pos: [50, 5], yaw: 90, speed_mps: 12 }] });
		expect(get(fleetVehicles)[0]).toMatchObject({ state: 'APPROACH_JUNCTION', pos: [50, 5], speed_mps: 12 });
		setErvState(2, { siren: false, destination: 'random' });
		expect(socket.sentJson().at(-1)).toEqual({ type: 'set_erv_state', actor_id: 2, siren: false, destination: 'random' });
		despawnFleetVehicle(2);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'despawn_v2x_vehicle', actor_id: 2 });
		despawnFleet();
		expect(socket.sentJson().at(-1)).toEqual({ type: 'despawn_v2x_vehicles' });
		socket.close();
		expect(get(fleetVehicles)).toEqual([]);
		expect(get(fleetEvents)).toEqual([]);
	});

	it('groups states into tones for the badges', () => {
		expect(stateTone('NORMAL')).toBe('calm');
		expect(stateTone('YIELD_PREP')).toBe('alert');
		expect(stateTone('PULL_OVER')).toBe('yield');
		expect(stateTone('STOPPED_HOLD')).toBe('stopped');
		expect(stateTone('ARRIVED')).toBe('done');
		expect(stateTone('SOMETHING_NEW')).toBe('erv');
	});
});
