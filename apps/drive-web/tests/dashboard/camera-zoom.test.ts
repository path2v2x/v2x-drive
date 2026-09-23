import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	birdAltitude,
	connect,
	disconnect,
	resetCameraZoom,
	zoomCamera
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
	birdAltitude.set(null);
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('camera_zoom protocol', () => {
	it('sends a camera_zoom packet with the multiplicative factor', () => {
		const socket = startDrive();
		zoomCamera(1.15);
		const command = JSON.parse(socket.sent.at(-1)!);
		expect(command).toEqual({ type: 'camera_zoom', factor: 1.15 });
	});

	it('sends a reset packet without a factor', () => {
		const socket = startDrive();
		resetCameraZoom();
		const command = JSON.parse(socket.sent.at(-1)!);
		expect(command).toEqual({ type: 'camera_zoom', reset: true });
	});

	it('updates the altitude store from the camera_zoomed acknowledgement', () => {
		const socket = startDrive();
		expect(get(birdAltitude)).toBeNull();

		socket.receive({ type: 'camera_zoomed', altitude: 28.75, min: 8, max: 100 });
		expect(get(birdAltitude)).toBe(28.75);
	});

	it('ignores camera_zoomed acknowledgements without a numeric altitude', () => {
		const socket = startDrive();
		socket.receive({ type: 'camera_zoomed', altitude: 'high' });
		expect(get(birdAltitude)).toBeNull();
	});
});
