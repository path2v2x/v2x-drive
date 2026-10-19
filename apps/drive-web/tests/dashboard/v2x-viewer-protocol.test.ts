import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { connect, disconnect, exportWireLog } from '$lib/stores/driveSocket';
import { resetWindowManager, windows } from '$lib/stores/windowManager';
import {
	LIST_WINDOW_ID,
	RING_MAX,
	closeTopV2xWindow,
	closeV2xStream,
	closeV2xViewer,
	clearV2xStream,
	exportV2xStream,
	flushV2xPending,
	groupV2xVehicles,
	openV2xStream,
	openV2xViewer,
	pauseV2xStream,
	requestV2xDecode,
	resetV2xViewer,
	setV2xDetail,
	streamWindowId,
	v2xDecoded,
	v2xDetail,
	v2xFeedStatus,
	v2xServerDropped,
	v2xStreams,
	v2xSubscribedKeys,
	v2xVehicles,
	v2xViewerOpen
} from '$lib/stores/v2xViewer';
import type { V2xFeedStatus, V2xMessageRow, V2xVehicleEntry } from '$lib/types';

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

	sentOfType(type: string): Record<string, unknown>[] {
		return this.sent.map((s) => JSON.parse(s)).filter((m) => m.type === type);
	}
}

function startSocket(): MockWebSocket {
	connect('wss://drive.example.test');
	const socket = MockWebSocket.instances.at(-1)!;
	socket.open();
	return socket;
}

function vehicle(key: string, kind: V2xVehicleEntry['kind'], extra: Partial<V2xVehicleEntry> = {}): V2xVehicleEntry {
	return {
		key,
		actor_id: 1,
		role: key,
		type_id: 'vehicle.lincoln.mkz',
		kind,
		subkind: 'scenario',
		org: key.split('-')[0],
		bsm_id: '11111110',
		pos: [0, 0, 0],
		yaw: 0,
		speed_mps: 0,
		emergency: false,
		special_lights: null,
		feed: { source: 'tena', last_rx: 1, age_s: 0.1, rate_hz: 10, count: 5 },
		in_world: true,
		...extra
	};
}

function feed(connected: boolean): V2xFeedStatus {
	return {
		decoder: { available: true, error: null },
		json_stream: { connected, records_per_s: connected ? 60 : 0 },
		udp: { datagrams_per_s: 0 },
		roster: { scenario: 'Cohort 2 - ERV', vehicles: 18 },
		hub: { total: 0, senders: 0, subscriptions: 0 },
		keeper: null
	};
}

function row(key: string, seq: number, tRx = 1000 + seq * 0.1): V2xMessageRow {
	return {
		key,
		seq,
		t_rx: tRx,
		msg_type: 'BSM',
		source: 'tena',
		sender: key,
		uuid: `u${seq}`,
		raw_hex: '0014' + (seq % 256).toString(16).padStart(2, '0'),
		size: 3,
		summary: null,
		error: null,
		tx: null
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetV2xViewer();
	resetWindowManager();
	localStorage.clear();
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('V2X viewer protocol', () => {
	it('opens the list window, requests the vehicle list and renews the watch lease', () => {
		const socket = startSocket();
		expect(get(v2xViewerOpen)).toBe(false);
		openV2xViewer();
		expect(get(v2xViewerOpen)).toBe(true);
		expect(socket.sentOfType('v2x_list_vehicles')).toHaveLength(1);

		socket.receive({
			type: 'v2x_vehicle_list',
			version: 3,
			ts: 1,
			feed: feed(true),
			vehicles: [
				vehicle('PATH-M-1', 'voices', { emergency: true, type_id: 'vehicle.ambulance.ford' }),
				vehicle('VW-M-1', 'ghost'),
				vehicle('PATH-BG-5', 'voices', { subkind: 'ego' })
			]
		});
		expect(get(v2xVehicles)).toHaveLength(3);
		expect(get(v2xFeedStatus)?.json_stream.connected).toBe(true);
		const groups = groupV2xVehicles(get(v2xVehicles));
		expect(groups.map((g) => [g.id, g.vehicles.map((v) => v.key)])).toEqual([
			['ego', ['PATH-BG-5']],
			['voices', ['PATH-M-1']],
			['ghost', ['VW-M-1']]
		]);

		vi.advanceTimersByTime(30_000);
		expect(socket.sentOfType('v2x_list_vehicles')).toHaveLength(2);
		closeV2xViewer();
		expect(get(v2xViewerOpen)).toBe(false);
		vi.advanceTimersByTime(60_000);
		expect(socket.sentOfType('v2x_list_vehicles')).toHaveLength(2);
	});

	it('subscribes per stream window and buffers rows in a bounded ring', () => {
		const socket = startSocket();
		openV2xStream('PATH-M-1');
		expect(JSON.parse(socket.sent.at(-1)!)).toEqual({ type: 'v2x_subscribe', keys: ['PATH-M-1'], detail: 'compact' });
		expect(get(windows).some((w) => w.id === streamWindowId('PATH-M-1'))).toBe(true);

		socket.receive({ type: 'v2x_messages', dropped: 0, batch: [row('PATH-M-1', 1), row('PATH-M-1', 2), row('OTHER', 3)] });
		expect(get(v2xStreams).get('PATH-M-1')?.rows).toHaveLength(0); // buffered until the flush tick
		vi.advanceTimersByTime(100);
		const stream = get(v2xStreams).get('PATH-M-1')!;
		expect(stream.rows.map((r) => r.seq)).toEqual([1, 2]);
		expect(stream.total).toBe(2);
		expect(stream.last?.seq).toBe(2);
		expect(stream.rateHz).toBe(2);
		expect(get(v2xStreams).has('OTHER')).toBe(false); // rows for unopened vehicles are ignored

		const many = Array.from({ length: 250 }, (_, i) => row('PATH-M-1', 10 + i));
		socket.receive({ type: 'v2x_messages', dropped: 7, batch: many });
		flushV2xPending();
		const after = get(v2xStreams).get('PATH-M-1')!;
		expect(after.rows).toHaveLength(RING_MAX);
		expect(after.rows[0].seq).toBe(10 + 250 - RING_MAX);
		expect(after.total).toBe(252);
		expect(get(v2xServerDropped)).toBe(7);

		const lines = exportV2xStream('PATH-M-1').trimEnd().split('\n');
		expect(lines).toHaveLength(RING_MAX);
		expect(JSON.parse(lines[0])).toMatchObject({ key: 'PATH-M-1', seq: after.rows[0].seq });

		pauseV2xStream('PATH-M-1');
		socket.receive({ type: 'v2x_messages', dropped: 0, batch: [row('PATH-M-1', 999)] });
		flushV2xPending();
		expect(get(v2xStreams).get('PATH-M-1')?.rows.at(-1)?.seq).not.toBe(999);
		expect(get(v2xStreams).get('PATH-M-1')?.total).toBe(253);
		pauseV2xStream('PATH-M-1', false);

		clearV2xStream('PATH-M-1');
		expect(get(v2xStreams).get('PATH-M-1')).toMatchObject({ rows: [], total: 0, last: null });

		closeV2xStream('PATH-M-1');
		expect(JSON.parse(socket.sent.at(-1)!)).toEqual({ type: 'v2x_unsubscribe', keys: ['PATH-M-1'] });
		expect(get(v2xStreams).has('PATH-M-1')).toBe(false);
		expect(get(windows)).toEqual([]);
		expect(exportV2xStream('PATH-M-1')).toBe('');
	});

	it('changes detail for every open stream and tracks the server acknowledgement', () => {
		const socket = startSocket();
		openV2xStream('A');
		openV2xStream('B');
		setV2xDetail('full');
		expect(JSON.parse(socket.sent.at(-1)!)).toEqual({ type: 'v2x_subscribe', keys: ['A', 'B'], detail: 'full' });
		socket.receive({ type: 'v2x_subscribed', keys: ['A', 'B'], detail: 'full', unknown: [] });
		expect(get(v2xSubscribedKeys)).toEqual(['A', 'B']);
		expect(get(v2xDetail)).toBe('full');
	});

	it('caches decode requests per frame', () => {
		const socket = startSocket();
		expect(requestV2xDecode('001431')).toBe(true);
		socket.receive({ type: 'v2x_decoded', raw_hex: '001431', msg_type: 'BSM', decoded: { messageId: 20 }, error: null });
		expect(get(v2xDecoded).get('001431')?.decoded).toEqual({ messageId: 20 });
		expect(requestV2xDecode('001431')).toBe(false);
		expect(requestV2xDecode('')).toBe(false);
		expect(socket.sentOfType('v2x_decode')).toHaveLength(1);
	});

	it('closes the topmost V2X window on Escape and resets when the socket closes', () => {
		const socket = startSocket();
		openV2xViewer();
		openV2xStream('A');
		expect(closeTopV2xWindow()).toBe(true);
		expect(get(windows).map((w) => w.id)).toEqual([LIST_WINDOW_ID]);
		expect(closeTopV2xWindow()).toBe(true);
		expect(closeTopV2xWindow()).toBe(false);

		openV2xStream('A');
		socket.receive({ type: 'v2x_messages', dropped: 0, batch: [row('A', 1)] });
		socket.close();
		expect(get(v2xStreams).size).toBe(0);
		expect(get(windows)).toEqual([]);
		vi.advanceTimersByTime(200); // a pending flush must not resurrect the stream
		expect(get(v2xStreams).size).toBe(0);
	});

	it('logs message batches in the wire log as a size marker', () => {
		const socket = startSocket();
		socket.receive({ type: 'v2x_messages', dropped: 0, batch: [] });
		// exportWireLog reads the buffer directly (the store itself flushes on a timer)
		const entries = exportWireLog()
			.trimEnd()
			.split('\n')
			.map((line) => JSON.parse(line) as { type: string; raw: string; bytes?: number });
		const entry = entries.find((p) => p.type === 'v2x_messages');
		expect(entry?.raw).toMatch(/^\[v2x_messages \d+ chars\]$/);
		expect(entry?.bytes).toBeGreaterThan(0);
	});
});
