import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import V2xVehiclePanel from '$lib/components/V2xVehiclePanel.svelte';
import { connect, disconnect, driveConnected, sessionState, voicesSlots } from '$lib/stores/driveSocket';
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
	voicesSlots.set([]);
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
		expect(socket.sentJson().slice(-2)).toEqual([{ type: 'list_v2x_vehicles' }, { type: 'list_voices_slots' }]);
		expect(screen.getByTestId('fleet-empty')).toBeInTheDocument();
		expect(screen.getByText('fleet status unknown')).toBeInTheDocument();
		expect(screen.queryByTestId('erv-slot')).toBeNull(); // no scenario slots known yet
	});

	it('offers the ambulance slot and spawns the ERV under it', async () => {
		voicesSlots.set([
			{ name: 'PATH-M-1', vehicle_type: 'vehicle.ambulance.ford', bsm_id: '11111110', emergency: true, state: 'free', holder: '', actor_id: null },
			{ name: 'PATH-BG-5', vehicle_type: 'vehicle.lincoln.mkz', bsm_id: '11111115', emergency: false, state: 'free', holder: '', actor_id: null }
		]);
		render(V2xVehiclePanel, { props: { onClose: () => {} } });
		const socket = MockWebSocket.instances.at(-1)!;
		await tick();
		const select = screen.getByTestId('erv-slot') as HTMLSelectElement;
		expect(select.value).toBe('PATH-M-1'); // preselected: free ambulance slot, civilian slots are not offered
		expect(Array.from(select.options).map((o) => o.text.trim())).toEqual(['local only (no BSMs)', 'PATH-M-1 · free']);
		await fireEvent.change(screen.getByLabelText('Emergency vehicle placement'), { target: { value: 'random' } });
		await fireEvent.click(screen.getByTestId('spawn-erv'));
		expect(socket.sentJson().at(-1)).toMatchObject({ type: 'spawn_v2x_vehicle', kind: 'erv', spawn: 'random', role: 'PATH-M-1' });
		expect(screen.getByRole('status').textContent).toContain('as PATH-M-1');
		// the slot got taken meanwhile (DT manual car): refused locally
		voicesSlots.set([
			{ name: 'PATH-M-1', vehicle_type: 'vehicle.ambulance.ford', bsm_id: '11111110', emergency: true, state: 'taken', holder: 'foreign', actor_id: 101 }
		]);
		await tick();
		const before = socket.sent.length;
		await fireEvent.click(screen.getByTestId('spawn-erv'));
		expect(socket.sent.length).toBe(before);
		expect(screen.getByRole('status').textContent).toContain('taken by the DT manual car');
		// local only still works
		await fireEvent.change(select, { target: { value: '' } });
		await fireEvent.click(screen.getByTestId('spawn-erv'));
		expect(socket.sentJson().at(-1)).not.toHaveProperty('role');
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
