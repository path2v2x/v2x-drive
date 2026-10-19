import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { connect, disconnect } from '$lib/stores/driveSocket';
import {
	bgTraffic,
	bgTrafficBusy,
	formatUptime,
	requestBgTraffic,
	resetBgTraffic,
	startBgTraffic,
	stopBgTraffic
} from '$lib/stores/bgTraffic';

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

const stopped = {
	type: 'bg_traffic', available: true, label: 'SUMO background traffic', state: 'stopped', running: false,
	since: null, uptime_s: null, last_start: null, last_stop: null, last_line: ''
};

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetBgTraffic();
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('background traffic protocol', () => {
	it('polls the status and sends start and stop', () => {
		connect('wss://drive.example.test');
		const socket = MockWebSocket.instances.at(-1)!;
		socket.open();

		requestBgTraffic();
		expect(socket.sentJson().at(-1)).toEqual({ type: 'bg_traffic_status' });
		socket.receive(stopped);
		expect(get(bgTraffic)?.state).toBe('stopped');

		startBgTraffic();
		expect(get(bgTrafficBusy)).toBe('start');
		expect(socket.sentJson().at(-1)).toEqual({ type: 'bg_traffic_start' });
		socket.receive({ ...stopped, state: 'starting', last_start: { at: 1, ok: true, code: 0, output: 'abc123' } });
		expect(get(bgTrafficBusy)).toBeNull();
		expect(get(bgTraffic)).toMatchObject({ state: 'starting', last_start: { ok: true } });

		socket.receive({ ...stopped, state: 'running', running: true, since: 10, uptime_s: 9661, last_line: '[x] sim 10 s' });
		stopBgTraffic();
		expect(get(bgTrafficBusy)).toBe('stop');
		expect(socket.sentJson().at(-1)).toEqual({ type: 'bg_traffic_stop' });
		socket.receive({ ...stopped, state: 'stopping', running: true });
		expect(get(bgTrafficBusy)).toBeNull();

		resetBgTraffic();
		expect(get(bgTraffic)).toBeNull();
	});

	it('formats uptimes', () => {
		expect(formatUptime(9661)).toBe('2 h 41 m');
		expect(formatUptime(125)).toBe('2 m 05 s');
		expect(formatUptime(7)).toBe('7 s');
		expect(formatUptime(null)).toBe('');
	});
});
