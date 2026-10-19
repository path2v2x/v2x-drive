import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import V2xVehiclePanel from '$lib/components/V2xVehiclePanel.svelte';
import { connect, disconnect, driveConnected, sessionState } from '$lib/stores/driveSocket';
import { fleetEvents, fleetStats, fleetTracks, fleetVehicles, resetFleetStore } from '$lib/stores/v2xFleet';
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

function erv(): V2xFleetVehicle {
	return {
		actor_id: 7, name: 'drive_erv_1', kind: 'erv', profile: 'ambulance', blueprint: 'vehicle.ambulance.ford', state: 'WAIT_FOR_YIELD',
		reason: 'waiting for the lead to yield', since_s: 2, pos: [1, 2], yaw: 0, speed_mps: 4.2, target_speed_mps: 8, offset_m: -0.3, stop: false,
		destination: [300, 0], dist_to_destination_m: 240, arrived: false, lane_change: null, passing: false, spawned_at: 1,
		siren: true, lights: true, response_type: 'emergency', manual: false, doors_open: false
	};
}

function civ(): V2xFleetVehicle {
	return {
		actor_id: 8, name: 'drive_civ_1', kind: 'civilian', profile: 'civilian', blueprint: 'vehicle.lincoln.mkz', state: 'STOPPED_HOLD',
		reason: 'stopped', since_s: 5, pos: [40, 0], yaw: 0, speed_mps: 0, target_speed_mps: 0, offset_m: 1.2, stop: true,
		destination: [500, 0], dist_to_destination_m: 460, arrived: false, lane_change: null, passing: false, spawned_at: 1, lights: ['hazard'], erv_key: 'actor:7'
	};
}

beforeEach(() => {
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetFleetStore();
	connect('wss://drive.example.test');
	MockWebSocket.instances.at(-1)!.open();
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
});

describe('V2xVehiclePanel', () => {
	it('asks for the fleet on mount and explains how to use it when empty', () => {
		render(V2xVehiclePanel, { props: { onClose: () => {} } });
		const socket = MockWebSocket.instances.at(-1)!;
		expect(socket.sentJson().at(-1)).toEqual({ type: 'list_v2x_vehicles' });
		expect(screen.getByTestId('fleet-empty')).toBeInTheDocument();
		expect(screen.getByText('fleet status unknown')).toBeInTheDocument();
	});

	it('refuses ego-relative spawns without a session and sends them with one', async () => {
		render(V2xVehiclePanel, { props: { onClose: () => {} } });
		const socket = MockWebSocket.instances.at(-1)!;
		await fireEvent.click(screen.getByTestId('spawn-civilians'));
		expect(screen.getByRole('status')).toHaveTextContent('Start a drive session first');
		expect(socket.sentJson().filter((m) => m.type === 'spawn_v2x_vehicle')).toHaveLength(0);
		driveConnected.set(true);
		sessionState.set('driving');
		await fireEvent.click(screen.getByTestId('spawn-civilians'));
		expect(socket.sentJson().filter((m) => m.type === 'spawn_v2x_vehicle')).toHaveLength(2);
		await fireEvent.click(screen.getByTestId('spawn-erv'));
		expect(socket.sentJson().at(-1)).toMatchObject({ kind: 'erv', profile: 'ambulance', spawn: { behind: 'ego', distance_m: 150 } });
	});

	it('lists vehicles with state badges and sends operator commands', async () => {
		fleetVehicles.set([erv(), civ()]);
		fleetTracks.set([{ key: 'actor:7', actor_id: 7, x: 1, y: 2, yaw: 0, speed_mps: 4.2, siren: true, lights: true, active: true,
			stationary: false, role: 'ambulance', response_type: 'emergency', source: 'fleet', age_s: 0.1, fleet_state: 'WAIT_FOR_YIELD' }]);
		fleetStats.set({ count: 2, max: 12, tick_ms_avg: 2.1, tick_ms_p95: 4.4, budget_ms: 20, degraded: false, decision_hz: 10, tracks: 1, events: 3 });
		fleetEvents.set([{ type: 'v2x_vehicle_event', actor_id: 8, name: 'drive_civ_1', kind: 'civilian', from: 'PULL_OVER', to: 'STOPPED_HOLD', reason: 'stopped', t: 1700000000 }]);
		render(V2xVehiclePanel, { props: { onClose: () => {} } });
		const socket = MockWebSocket.instances.at(-1)!;
		expect(screen.getAllByTestId('fleet-vehicle')).toHaveLength(2);
		expect(screen.getAllByTestId('fleet-state').map((e) => e.textContent)).toEqual(['WAIT_FOR_YIELD', 'STOPPED_HOLD']);
		expect(screen.getByTestId('fleet-track')).toHaveTextContent('actor:7');
		expect(screen.getByTestId('fleet-event')).toHaveTextContent('PULL_OVER → STOPPED_HOLD');
		expect(screen.getByText('2/12 vehicles')).toBeInTheDocument();

		await fireEvent.click(screen.getByRole('button', { name: 'siren' }));
		expect(socket.sentJson().at(-1)).toEqual({ type: 'set_erv_state', actor_id: 7, siren: false });
		await fireEvent.click(screen.getByRole('button', { name: 'hold' }));
		expect(socket.sentJson().at(-1)).toEqual({ type: 'set_erv_state', actor_id: 7, manual: true });
		await fireEvent.click(screen.getByRole('button', { name: 'arrived' }));
		expect(socket.sentJson().at(-1)).toEqual({ type: 'set_erv_state', actor_id: 7, arrived: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Despawn drive_civ_1' }));
		expect(socket.sentJson().at(-1)).toEqual({ type: 'despawn_v2x_vehicle', actor_id: 8 });
		await fireEvent.click(screen.getByRole('button', { name: 'Despawn all' }));
		expect(socket.sentJson().at(-1)).toEqual({ type: 'despawn_v2x_vehicles' });
	});
});
