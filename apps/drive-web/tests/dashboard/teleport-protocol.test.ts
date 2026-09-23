import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	connect,
	disconnect,
	resetTeleportStatus,
	telemetry,
	teleportStatus,
	teleportVehicle
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
}

function startDrive(): MockWebSocket {
	connect('wss://drive.example.test');
	const socket = MockWebSocket.instances.at(-1)!;
	socket.open();
	socket.receive({ type: 'session_ready', vehicle_id: 1, objects_count: 0 });
	return socket;
}

beforeEach(() => {
	vi.useFakeTimers();
	MockWebSocket.instances = [];
	vi.stubGlobal('WebSocket', MockWebSocket);
	resetTeleportStatus();
	telemetry.set({
		speed: 0,
		gear: 0,
		pos: [1, 2, 3],
		rot: [0, 0, 0],
		steer: 0,
		throttle: 0,
		brake: 0
	});
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('typed Teleport protocol', () => {
	it('does not create duplicate sockets while a connection is pending', () => {
		connect('wss://drive.example.test');
		connect('wss://drive.example.test');
		expect(MockWebSocket.instances).toHaveLength(1);
	});

	it('stays pending and leaves telemetry untouched until a valid acknowledgement', () => {
		const socket = startDrive();
		expect(teleportVehicle(10, 20)).toBe(true);
		const command = JSON.parse(socket.sent.at(-1)!);
		expect(command).toMatchObject({ type: 'teleport', x: 10, y: 20 });
		expect(command.request_id).toEqual(expect.any(String));
		expect(get(teleportStatus).state).toBe('pending');
		expect(get(telemetry).pos).toEqual([1, 2, 3]);

		socket.receive({
			type: 'teleported',
			request_id: command.request_id,
			success: true,
			pos: [10, 20, 4.5]
		});
		expect(get(teleportStatus)).toMatchObject({ state: 'succeeded', pos: [10, 20, 4.5] });
		expect(get(telemetry).pos).toEqual([10, 20, 4.5]);
	});

	it('surfaces typed server validation errors', () => {
		const socket = startDrive();
		teleportVehicle(10, 20, 4, 90);
		const command = JSON.parse(socket.sent.at(-1)!);
		socket.receive({
			type: 'teleport_error',
			request_id: command.request_id,
			success: false,
			message: 'outside active map'
		});
		expect(get(teleportStatus)).toEqual({
			state: 'error',
			message: 'outside active map',
			pos: null
		});
	});

	it('rejects malformed acknowledgements and unsafe client coordinates', () => {
		const socket = startDrive();
		expect(teleportVehicle(Number.NaN, 20)).toBe(false);
		expect(socket.sent).toHaveLength(0);
		expect(get(teleportStatus).state).toBe('error');

		resetTeleportStatus();
		expect(teleportVehicle(10, 20)).toBe(true);
		const command = JSON.parse(socket.sent.at(-1)!);
		socket.receive({
			type: 'teleported',
			request_id: command.request_id,
			success: true,
			pos: ['bad', 20, 3]
		});
		expect(get(teleportStatus).state).toBe('error');
	});

	it('ignores late or mismatched acknowledgements', () => {
		const socket = startDrive();
		teleportVehicle(10, 20);
		const command = JSON.parse(socket.sent.at(-1)!);
		socket.receive({
			type: 'teleported',
			request_id: `${command.request_id}-old`,
			success: true,
			pos: [99, 99, 99]
		});
		expect(get(teleportStatus).state).toBe('pending');
		expect(get(telemetry).pos).toEqual([1, 2, 3]);

		socket.receive({
			type: 'teleported',
			request_id: command.request_id,
			success: true,
			pos: [10, 20, 3]
		});
		expect(get(teleportStatus).state).toBe('succeeded');
	});

	it('ignores callbacks from a replaced socket', () => {
		const oldSocket = startDrive();
		teleportVehicle(10, 20);
		const oldCommand = JSON.parse(oldSocket.sent.at(-1)!);
		disconnect();

		const currentSocket = startDrive();
		teleportVehicle(30, 40);
		const currentCommand = JSON.parse(currentSocket.sent.at(-1)!);
		oldSocket.receive({
			type: 'teleported',
			request_id: oldCommand.request_id,
			success: true,
			pos: [10, 20, 3]
		});
		expect(get(teleportStatus).state).toBe('pending');
		expect(get(telemetry).pos).toEqual([1, 2, 3]);

		currentSocket.receive({
			type: 'teleported',
			request_id: currentCommand.request_id,
			success: true,
			pos: [30, 40, 3]
		});
		expect(get(teleportStatus).state).toBe('succeeded');
	});

	it('turns a missing acknowledgement into an explicit timeout error', () => {
		startDrive();
		teleportVehicle(10, 20);
		vi.advanceTimersByTime(10_000);
		expect(get(teleportStatus).state).toBe('error');
		expect(get(teleportStatus).message).toContain('timed out');
	});
});
