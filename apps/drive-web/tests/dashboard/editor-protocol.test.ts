import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { connect, disconnect, sessionMode, sessionState, startEditorSession, startSession } from '$lib/stores/driveSocket';
import {
	actorDetails,
	cameraPose,
	distanceFromCamera,
	focusActor,
	followActor,
	followingId,
	requestWorldActors,
	resetEditorStore,
	selectActor,
	selectedActorId,
	sendCameraMove,
	worldActors,
	worldActorsInfo
} from '$lib/stores/editor';

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
	resetEditorStore();
});

afterEach(() => {
	disconnect();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('editor protocol', () => {
	it('starts an editor session and keeps the camera pose, the outliner and the details', () => {
		const socket = startSocket();
		startEditorSession({ x: 10, yaw: 90 });
		expect(get(sessionState)).toBe('reconstructing');
		expect(socket.sentJson().at(-1)).toEqual({ type: 'start_session', mode: 'editor', pose: { x: 10, yaw: 90 } });

		socket.receive({
			type: 'session_ready', mode: 'editor', vehicle_id: null, objects_count: 0, sensor_actor_ids: [9], ego_role: '',
			camera: { type: 'camera_pose', x: 10, y: 0, z: 30, yaw: 90, pitch: -35, speed: 20, following: null }
		});
		expect(get(sessionState)).toBe('driving');
		expect(get(sessionMode)).toBe('editor');
		expect(get(cameraPose)).toMatchObject({ x: 10, z: 30, speed: 20 });

		expect(sendCameraMove({ type: 'camera_move', f: 1, dt: 0.05 })).toBe(true);
		socket.receive({ type: 'camera_pose', x: 11, y: 0, z: 30, yaw: 90, pitch: -35, speed: 20, following: null });
		expect(get(cameraPose)?.x).toBe(11);

		expect(requestWorldActors(50)).toBe(true);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'list_world_actors', max: 50 });
		socket.receive({
			type: 'world_actors', total: 2, truncated: false, groups: { voices: 1, sensor: 1 }, following: null,
			actors: [
				{ id: 5, type_id: 'vehicle.ambulance.ford', name: 'Ambulance Ford', role: 'PATH-M-1', group: 'voices', owner: 'fleet', pos: [14, 3, 0], yaw: 0, speed_mps: 0, parent: null, alive: true },
				{ id: 9, type_id: 'sensor.camera.rgb', name: 'Camera Rgb', role: 'drive_editor_1', group: 'sensor', owner: 'drive', pos: null, yaw: null, speed_mps: null, parent: null, alive: true }
			]
		});
		expect(get(worldActors).map((a) => a.id)).toEqual([5, 9]);
		expect(get(worldActorsInfo)).toMatchObject({ total: 2, truncated: false, groups: { voices: 1 } });
		expect(distanceFromCamera(get(cameraPose), get(worldActors)[0])).toBeCloseTo(Math.hypot(3, 3, 30), 5);
		expect(distanceFromCamera(get(cameraPose), get(worldActors)[1])).toBeNull();

		selectActor(5);
		expect(get(selectedActorId)).toBe(5);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'inspect_actor', actor_id: 5 });
		socket.receive({ type: 'actor_details', id: 5, name: 'Ambulance Ford', group: 'voices', attributes: { role_name: 'PATH-M-1' }, lights: ['Special1'] });
		expect(get(actorDetails)).toMatchObject({ id: 5, lights: ['Special1'] });
		selectActor(null);
		expect(get(actorDetails)).toBeNull();

		focusActor(5);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'camera_focus', actor_id: 5 });
		followActor(5);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'camera_follow', actor_id: 5 });
		socket.receive({ type: 'camera_pose', x: 11, y: 0, z: 30, yaw: 90, pitch: -35, speed: 20, following: 5 });
		expect(get(followingId)).toBe(5);
		followActor(null);
		expect(socket.sentJson().at(-1)).toEqual({ type: 'camera_follow', actor_id: null });

		// a driving start carries the requested spawn pose and resets the mode
		socket.receive({ type: 'session_ended' });
		startSession('a', 'b', 'vehicle.tesla.model3', '', { x: 1, y: 2, yaw: 3 });
		expect(socket.sentJson().at(-1)).toMatchObject({ type: 'start_session', spawn: { x: 1, y: 2, yaw: 3 }, voices_role: '' });
		expect(get(sessionMode)).toBe('driving');
	});
});
