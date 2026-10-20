import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import V2xPerspectivePanel from '$lib/components/V2xPerspectivePanel.svelte';
import { connect, disconnect } from '$lib/stores/driveSocket';
import {
	DEFAULT_RADIUS_M,
	formatDistance,
	relBearingLabel,
	resetV2xPerspective,
	setV2xRadius,
	subscribeV2xPerspective,
	toggleV2xExpanded,
	txLine,
	unsubscribeV2xPerspective,
	v2xExpanded,
	v2xPerspective,
	v2xPerspectiveActive,
	v2xRadiusM
} from '$lib/stores/v2xPerspective';
import type { V2xPerspectiveMessage } from '$lib/types';

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

const bsm = (id: string, speed: number, heading: number) => ({
	id, msg_cnt: 7, sec_mark: 12000, lat: 0.001, lon: 0.002, elev_m: null, speed_mps: speed, heading_deg: heading,
	brakes: false, transmission: null, size_m: null, lights: null, coords: 'origin-relative' as const
});

const snapshot: V2xPerspectiveMessage = {
	type: 'v2x_perspective', active: true, ts: 1, radius_m: 300, interval_ms: 500, count: 2, transmitting: 2,
	vehicles: [
		{
			key: 'PATH-M-1', actor_id: 161, name: 'PATH-M-1', kind: 'voices', subkind: 'scenario', org: 'PATH', emergency: true,
			pos: [0, 0, 0], yaw: 0, speed_mps: 12,
			tx: { msg_type: 'BSM', t_rx: 1, age_s: 0.1, rate_hz: 10, count: 50, source: 'tena', size: 52, summary: bsm('11111110', 12, 90), error: null },
			rx: [{ key: 'PATH-BG-1', name: 'PATH-BG-1', actor_id: 157, kind: 'voices', org: 'PATH', emergency: false, distance_m: 100,
				bearing_deg: 0, rel_bearing_deg: 0, msg_type: 'BSM', age_s: 0.2, rate_hz: 10, source: 'tena', summary: bsm('11111111', 8, 90) }],
			rx_count: 1
		},
		{
			key: 'PATH-BG-1', actor_id: 157, name: 'PATH-BG-1', kind: 'voices', subkind: 'scenario', org: 'PATH', emergency: false,
			pos: [100, 0, 0], yaw: 180, speed_mps: 8,
			tx: { msg_type: 'BSM', t_rx: 1, age_s: 0.2, rate_hz: 10, count: 50, source: 'tena', size: 52, summary: bsm('11111111', 8, 90), error: null },
			rx: [{ key: 'PATH-M-1', name: 'PATH-M-1', actor_id: 161, kind: 'voices', org: 'PATH', emergency: true, distance_m: 100,
				bearing_deg: 180, rel_bearing_deg: 0, msg_type: 'BSM', age_s: 0.1, rate_hz: 10, source: 'tena', summary: bsm('11111110', 12, 90) }],
			rx_count: 1
		}
	]
};

function startSocket(): MockWebSocket {
	connect('wss://drive.example.test');
	const socket = MockWebSocket.instances.at(-1)!;
	socket.open();
	return socket;
}

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetV2xPerspective();
	v2xRadiusM.set(DEFAULT_RADIUS_M);
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('v2x perspective store', () => {
	it('subscribes with the radius, keeps the snapshot and clears it on unsubscribe or disconnect', () => {
		const socket = startSocket();
		subscribeV2xPerspective();
		expect(socket.sentJson().at(-1)).toEqual({ type: 'v2x_perspective_subscribe', radius_m: 300, interval_ms: 500 });

		socket.receive(snapshot);
		expect(get(v2xPerspectiveActive)).toBe(true);
		expect(get(v2xPerspective)?.vehicles.map((v) => v.key)).toEqual(['PATH-M-1', 'PATH-BG-1']);

		setV2xRadius(150.4);
		expect(get(v2xRadiusM)).toBe(150);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'v2x_perspective_set', radius_m: 150 });
		setV2xRadius(1);
		expect(get(v2xRadiusM)).toBe(10);

		toggleV2xExpanded('PATH-M-1');
		expect(get(v2xExpanded).has('PATH-M-1')).toBe(true);
		toggleV2xExpanded('PATH-M-1');
		expect(get(v2xExpanded).has('PATH-M-1')).toBe(false);

		unsubscribeV2xPerspective();
		expect(socket.sentJson().at(-1)).toEqual({ type: 'v2x_perspective_unsubscribe' });
		socket.receive({ type: 'v2x_perspective', active: false, vehicles: [], count: 0, radius_m: null });
		expect(get(v2xPerspectiveActive)).toBe(false);
		expect(get(v2xPerspective)).toBeNull();

		socket.receive(snapshot);
		expect(get(v2xPerspective)).not.toBeNull();
		socket.close();
		expect(get(v2xPerspective)).toBeNull();
	});

	it('formats bearings, distances and the transmit line', () => {
		expect(relBearingLabel(0)).toBe('ahead');
		expect(relBearingLabel(-25)).toBe('ahead');
		expect(relBearingLabel(90)).toBe('right');
		expect(relBearingLabel(-60)).toBe('left');
		expect(relBearingLabel(170)).toBe('behind');
		expect(relBearingLabel(null)).toBe('');
		expect(formatDistance(99.6)).toBe('100 m');
		expect(formatDistance(1250)).toBe('1.3 km');
		expect(formatDistance(null)).toBe('–');
		expect(txLine(null)).toBe('nothing');
		expect(txLine(snapshot.vehicles[0].tx)).toBe('43 km/h · 90°');
	});
});

describe('V2xPerspectivePanel', () => {
	it('lists the vehicles, expands one to its sends and hears blocks, and changes the radius', async () => {
		const socket = startSocket();
		render(V2xPerspectivePanel);
		await tick();
		expect(socket.sentJson().some((m) => m.type === 'v2x_perspective_subscribe')).toBe(true);
		expect(screen.getByText('waiting for the V2X feed')).toBeTruthy();

		socket.receive(snapshot);
		await tick();
		const rows = screen.getAllByTestId('v2x-vehicle');
		expect(rows).toHaveLength(2);
		expect(screen.getByText('V2X · 2 vehicles')).toBeTruthy();
		expect(screen.getAllByTestId('v2x-vehicle-summary')[0].textContent).toContain('10 Hz · hears 1');
		expect(screen.queryByTestId('v2x-vehicle-details')).toBeNull();

		await fireEvent.click(screen.getAllByTestId('v2x-vehicle-toggle')[0]);
		await tick();
		const details = screen.getByTestId('v2x-vehicle-details');
		expect(screen.getByTestId('v2x-tx-line').textContent).toBe('43 km/h · 90°');
		expect(details.textContent).not.toContain('11111110'); // the raw ids stay out of the panel
		const heard = screen.getAllByTestId('v2x-rx-row');
		expect(heard).toHaveLength(1);
		expect(screen.getByTestId('v2x-rx-distance').textContent).toBe('100 m');
		expect(heard[0].textContent).toContain('ahead');
		expect(heard[0].textContent).toContain('PATH-BG-1');
		expect(heard[0].textContent).toContain('29 km/h');

		// a new snapshot replaces the rows in place: same row, new distance
		const moved = JSON.parse(JSON.stringify(snapshot)) as V2xPerspectiveMessage;
		moved.vehicles[0].rx[0].distance_m = 42;
		socket.receive(moved);
		await tick();
		expect(screen.getAllByTestId('v2x-rx-row')).toHaveLength(1);
		expect(screen.getByTestId('v2x-rx-distance').textContent).toBe('42 m');

		const radius = screen.getByTestId('v2x-radius') as HTMLSelectElement;
		expect([...radius.options].map((o) => o.value)).toEqual(['50', '100', '200', '300', '500', '1000', '2000']);
		await fireEvent.change(radius, { target: { value: '100' } });
		expect(socket.sentJson().at(-1)).toEqual({ type: 'v2x_perspective_set', radius_m: 100 });
		expect(get(v2xRadiusM)).toBe(100);
	});
});
