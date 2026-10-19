import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	connect,
	disconnect,
	egoVoicesRole,
	requestVoicesSlots,
	startSession,
	voicesSlots,
	voicesSlotsInfo
} from '$lib/stores/driveSocket';

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

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('VOICES slots protocol', () => {
	it('requests the slot list, keeps it, and sends the chosen slot with start_session', () => {
		const socket = startSocket();
		expect(requestVoicesSlots()).toBe(true);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'list_voices_slots' });

		socket.receive({
			type: 'voices_slots',
			available: true,
			default: 'PATH-BG-5',
			local_org: 'PATH',
			scenario: 'Cohort 2 - ERV',
			slots: [
				{ name: 'PATH-M-1', vehicle_type: 'vehicle.ambulance.ford', bsm_id: '11111110', emergency: true, state: 'taken', holder: 'foreign', actor_id: 101 },
				{ name: 'PATH-BG-5', vehicle_type: 'vehicle.lincoln.mkz', bsm_id: '11111115', emergency: false, state: 'free', holder: '', actor_id: null }
			]
		});
		expect(get(voicesSlots).map((s) => `${s.name}:${s.state}`)).toEqual(['PATH-M-1:taken', 'PATH-BG-5:free']);
		expect(get(voicesSlotsInfo)).toEqual({ available: true, default: 'PATH-BG-5', local_org: 'PATH', scenario: 'Cohort 2 - ERV' });

		startSession('2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 'vehicle.tesla.model3', 'PATH-BG-5');
		expect(socket.sentJson().at(-1)).toEqual({
			type: 'start_session',
			start: '2026-01-01T00:00:00Z',
			end: '2026-01-01T01:00:00Z',
			vehicle: 'vehicle.tesla.model3',
			voices_role: 'PATH-BG-5'
		});
		socket.receive({ type: 'session_ready', vehicle_id: 7, objects_count: 0, ego_role: 'PATH-BG-5', voices_role: 'PATH-BG-5' });
		expect(get(egoVoicesRole)).toBe('PATH-BG-5');
		socket.receive({ type: 'session_ended' });
		expect(get(egoVoicesRole)).toBe('');

		// no choice: the server default applies; an empty string asks for a plain car
		startSession('a', 'b', 'vehicle.tesla.model3');
		expect(socket.sentJson().at(-1)).not.toHaveProperty('voices_role');
		socket.receive({ type: 'session_ended' });
		startSession('a', 'b', 'vehicle.tesla.model3', '');
		expect(socket.sentJson().at(-1)).toMatchObject({ voices_role: '' });
		socket.receive({ type: 'session_ready', vehicle_id: 8, objects_count: 0, ego_role: 'ego_vehicle_1' });
		expect(get(egoVoicesRole)).toBe('');

		// a malformed list leaves the stores sane
		socket.receive({ type: 'voices_slots', slots: null, available: false });
		expect(get(voicesSlots)).toEqual([]);
		expect(get(voicesSlotsInfo)).toEqual({ available: false, default: '', local_org: '', scenario: '' });
	});
});
