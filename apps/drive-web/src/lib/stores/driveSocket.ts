/**
 * Drive WebSocket Store — manages the connection to the drive server.
 *
 * Handles: connection lifecycle, session management, control message sending,
 * telemetry reception, and auto-reconnect.
 */

import { writable, get } from 'svelte/store';
import type {
	DriveSessionState,
	VehicleTelemetry,
	CameraView,
	DriveMessage,
	VehicleOption,
	SpawnableObject,
	PlacedObject,
	ScenarioInfo,
	V2xSignal,
	V2xAlert,
	V2xZone,
	TrajectoryInfo,
	TrajectoryStatus,
	XoscScenarioInfo,
	XoscRunnerStatus,
	XoscEvent,
	XoscFinishedEvent,
	DynamicActor,
	DriveMapId,
	DriveMapOption,
	TeleportCommand,
	TeleportErrorMessage,
	TeleportedMessage,
	TeleportStatus
} from '$lib/types';
import { v2xZones } from './v2xZones';

// ── Stores ──

export const driveConnected = writable<boolean>(false);

// Callback for binary frames (MJPEG). Set by the drive page to push frames to CameraView.
let onFrameCallback: ((data: Blob) => void) | null = null;

export function setOnFrame(cb: ((data: Blob) => void) | null): void {
	onFrameCallback = cb;
}
export const sessionState = writable<DriveSessionState>('idle');
export const telemetry = writable<VehicleTelemetry>({
	speed: 0,
	gear: 0,
	pos: [0, 0, 0],
	rot: [0, 0, 0],
	steer: 0,
	throttle: 0,
	brake: 0,
});
export const lastError = writable<string | null>(null);
export const vehicleId = writable<number | null>(null);
export const objectsCount = writable<number>(0);
export const vehicleList = writable<VehicleOption[]>([]);
export const driveMaps = writable<DriveMapOption[]>([]);
export const currentDriveMap = writable<DriveMapId | null>(null);
export const mapSwitching = writable<boolean>(false);
export const spawnableObjects = writable<SpawnableObject[]>([]);
export const placedObjects = writable<PlacedObject[]>([]);
export const placedCount = writable<number>(0);
export const scenarioList = writable<ScenarioInfo[]>([]);
export const v2xSignals = writable<V2xSignal[]>([]);
export const v2xSignalCount = writable<number>(0);
export const v2xAlerts = writable<V2xAlert[]>([]);
export const trajectoryList = writable<TrajectoryInfo[]>([]);
export const trajectoryStatus = writable<TrajectoryStatus>({ active: false });
export const dynamicActors = writable<DynamicActor[]>([]);
export const teleportStatus = writable<TeleportStatus>({
	state: 'idle',
	message: null,
	pos: null
});
// Aspect ratio of the streamed ego camera (updated by CameraSettingsPanel).
export const cameraAspect = writable<{ w: number; h: number }>({ w: 720, h: 720 });
// Bird's-eye camera altitude in meters (null until the server acks a zoom).
export const birdAltitude = writable<number | null>(null);

// OpenSCENARIO (.xosc) state
export const xoscScenarioList = writable<XoscScenarioInfo[]>([]);
export const xoscRunnerStatus = writable<XoscRunnerStatus>({
	running: false,
	file: null,
	scenario_runner_configured: false,
});
export const xoscEventLog = writable<XoscEvent[]>([]);
export const xoscLastResult = writable<XoscFinishedEvent | null>(null);

const XOSC_LOG_MAX = 500;
const TELEPORT_ACK_TIMEOUT_MS = 10_000;
export const TELEPORT_COORD_ABS_LIMIT_M = 100_000;
export const TELEPORT_MIN_Z_M = -20;
export const TELEPORT_MAX_Z_M = 500;
export const TELEPORT_MAX_ABS_YAW_DEG = 360;
let teleportAckTimer: ReturnType<typeof setTimeout> | null = null;
let activeTeleportRequestId: string | null = null;
let teleportRequestSequence = 0;

function newTeleportRequestId(): string {
	if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
	teleportRequestSequence += 1;
	return `teleport-${Date.now().toString(36)}-${teleportRequestSequence.toString(36)}`;
}

function clearTeleportAckTimer(): void {
	if (teleportAckTimer) {
		clearTimeout(teleportAckTimer);
		teleportAckTimer = null;
	}
}

function setTeleportError(message: string): void {
	clearTeleportAckTimer();
	activeTeleportRequestId = null;
	teleportStatus.set({ state: 'error', message, pos: null });
}

export function resetTeleportStatus(): void {
	clearTeleportAckTimer();
	activeTeleportRequestId = null;
	teleportStatus.set({ state: 'idle', message: null, pos: null });
}

function isPosition(value: unknown): value is [number, number, number] {
	return Array.isArray(value)
		&& value.length === 3
		&& value.every((coordinate) => typeof coordinate === 'number' && Number.isFinite(coordinate));
}

function isTeleportedMessage(msg: DriveMessage): msg is TeleportedMessage {
	return msg.type === 'teleported'
		&& typeof msg.request_id === 'string'
		&& msg.success === true
		&& isPosition(msg.pos);
}

function isTeleportErrorMessage(msg: DriveMessage): msg is TeleportErrorMessage {
	return msg.type === 'teleport_error'
		&& typeof msg.request_id === 'string'
		&& msg.success === false
		&& typeof msg.message === 'string';
}

// ── Wire log (live packet inspector) ──
// Records every frame on the socket verbatim: exact TX/RX JSON strings,
// binary frames as size markers, plus connection lifecycle events.

export type WirePacket = {
	i: number;
	t: string;
	dir: 'TX' | 'RX' | 'EVT';
	kind: 'json' | 'bin' | 'evt';
	ptype: string;
	data: string;
	bytes?: number;
};

const WIRE_CAP = 3000;
export const wireLog = writable<WirePacket[]>([]);
export const wirePaused = writable<boolean>(false);
export const wireTotal = writable<number>(0);
let wireBuf: WirePacket[] = [];
let wireSeq = 0;
let wireCount = 0;
let wireDirty = false;
let wireFlushTimer: ReturnType<typeof setInterval> | null = null;

function wireTimestamp(): string {
	const d = new Date();
	return d.toLocaleTimeString([], { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function recordWire(
	dir: WirePacket['dir'],
	kind: WirePacket['kind'],
	ptype: string,
	data: string,
	bytes?: number
): void {
	wireCount += 1;
	wireBuf.push({ i: wireSeq++, t: wireTimestamp(), dir, kind, ptype, data, bytes });
	if (wireBuf.length > WIRE_CAP) {
		// Evict the oldest high-frequency entry first so one-shot commands
		// (start_session, session_ready, teleport…) survive the control/
		// telemetry/frame flood instead of scrolling out of the buffer.
		const evictIdx = wireBuf.findIndex(
			(p) =>
				p.ptype === 'control' ||
				p.ptype === 'telemetry' ||
				p.ptype === 'camera_zoom' ||
				p.ptype === 'camera_zoomed' ||
				p.kind === 'bin'
		);
		wireBuf.splice(evictIdx === -1 ? 0 : evictIdx, 1);
	}
	wireDirty = true;
	if (!wireFlushTimer) {
		// Batch store updates (~3/s) so 40 pkt/s doesn't thrash subscribers.
		wireFlushTimer = setInterval(() => {
			if (!wireDirty || get(wirePaused)) return;
			wireDirty = false;
			wireLog.set([...wireBuf]);
			wireTotal.set(wireCount);
		}, 300);
	}
}

export function clearWireLog(): void {
	wireBuf = [];
	wireSeq = 0;
	wireCount = 0;
	wireDirty = false;
	wireLog.set([]);
	wireTotal.set(0);
}

/** Full buffer as JSON Lines — `raw` is the exact string that crossed the wire. */
export function exportWireLog(): string {
	return (
		wireBuf
			.map((p) =>
				JSON.stringify({
					t: p.t,
					dir: p.dir,
					kind: p.kind,
					type: p.ptype,
					...(p.bytes !== undefined ? { bytes: p.bytes } : {}),
					raw: p.data
				})
			)
			.join('\n') + '\n'
	);
}

// ── WebSocket ──

let ws: WebSocket | null = null;

export function connect(wsUrl: string): void {
	if (
		ws
		&& (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)
	) return;

	sessionState.set('connecting');
	lastError.set(null);
	resetTeleportStatus();

	let socket: WebSocket;
	try {
		socket = new WebSocket(wsUrl);
	} catch {
		sessionState.set('error');
		lastError.set('Invalid Drive WebSocket URL');
		return;
	}
	ws = socket;
	socket.binaryType = 'blob';

	socket.onopen = () => {
		if (ws !== socket) return;
		recordWire('EVT', 'evt', 'open', `connection OPEN → ${wsUrl}`);
		driveConnected.set(true);
		console.log('[DriveWS] Connected');
	};

	socket.onmessage = (event) => {
		if (ws !== socket) return;
		// Binary message = JPEG camera frame
		if (event.data instanceof Blob) {
			recordWire('RX', 'bin', 'frame', '[binary JPEG camera frame]', event.data.size);
			if (onFrameCallback) {
				onFrameCallback(event.data);
			}
			return;
		}

		// Text message = JSON (telemetry, session events, errors)
		try {
			const msg: DriveMessage = JSON.parse(event.data);
			recordWire('RX', 'json', typeof msg.type === 'string' ? msg.type : '?', event.data);
			handleServerMessage(msg);
		} catch (e) {
			recordWire('RX', 'json', 'unparsed', String(event.data));
			console.warn('[DriveWS] Invalid message:', event.data);
		}
	};

	socket.onclose = () => {
		if (ws !== socket) return;
		recordWire('EVT', 'evt', 'close', 'connection CLOSED');
		ws = null;
		driveConnected.set(false);
		console.log('[DriveWS] Disconnected');
		if (get(teleportStatus).state === 'pending') {
			setTeleportError('Connection closed before the teleport was acknowledged.');
		}

		// Don't auto-reconnect — it creates zombie state where the frontend
		// thinks it has a session but the server already cleaned up.
		// Just reset to idle and let the user start fresh.
		const state = get(sessionState);
		if (state !== 'idle') {
			console.log('[DriveWS] Session lost — resetting to idle');
			sessionState.set('idle');
			vehicleId.set(null);
			dynamicActors.set([]);
		}
	};

	socket.onerror = (e) => {
		if (ws !== socket) return;
		recordWire('EVT', 'evt', 'error', 'socket error');
		console.error('[DriveWS] Error:', e);
		lastError.set('WebSocket connection error');
	};
}

export function disconnect(): void {
	if (get(teleportStatus).state === 'pending') {
		setTeleportError('Teleport cancelled because the drive connection closed.');
	}
	if (ws) {
		const socket = ws;
		ws = null;
		socket.close();
	}
	driveConnected.set(false);
	sessionState.set('idle');
	vehicleId.set(null);
	dynamicActors.set([]);
}

// ── Message Handling ──

function handleServerMessage(msg: DriveMessage): void {
	switch (msg.type) {
		case 'session_ready':
			sessionState.set('driving');
			vehicleId.set(msg.vehicle_id as number);
			objectsCount.set(msg.objects_count as number);
			break;

		case 'telemetry':
			telemetry.set(msg as unknown as VehicleTelemetry);
			dynamicActors.set(Array.isArray(msg.dynamic_actors) ? (msg.dynamic_actors as DynamicActor[]) : []);
			// If we receive telemetry, we're actively driving
			if (get(sessionState) === 'ready') {
				sessionState.set('driving');
			}
			// Handle V2X proximity alerts from telemetry. Bridge re-broadcasts
			// every tick, so we dedup by `id` here: an incoming alert with an
			// id we already have updates that toast's distance in place; new
			// ids become a fresh toast. `_lastSeen` is set every update so
			// V2xToast can auto-dismiss alerts that stop arriving.
			if (msg.v2x_alerts) {
				const incoming = msg.v2x_alerts as V2xAlert[];
				const now = Date.now();
				v2xAlerts.update(existing => {
					const byId = new Map<number, V2xAlert>();
					for (const e of existing) byId.set(e.id, e);
					for (const a of incoming) {
						const prev = byId.get(a.id);
						if (prev) {
							byId.set(a.id, { ...prev, ...a, _lastSeen: now } as V2xAlert);
						} else {
							byId.set(a.id, { ...a, _uid: now + Math.random(), _lastSeen: now } as V2xAlert);
						}
					}
					return Array.from(byId.values());
				});
			}
			break;

		case 'session_ended':
			if (get(teleportStatus).state === 'pending') {
				setTeleportError('Drive session ended before the teleport was acknowledged.');
			}
			sessionState.set('idle');
			vehicleId.set(null);
			dynamicActors.set([]);
			break;

		case 'vehicle_list':
			vehicleList.set((msg.vehicles as VehicleOption[]) ?? []);
			break;

		case 'map_status':
			driveMaps.set((msg.maps as DriveMapOption[]) ?? []);
			currentDriveMap.set((msg.current_map as DriveMapId | null) ?? null);
			mapSwitching.set(false);
			break;

		case 'map_set':
			driveMaps.set((msg.maps as DriveMapOption[]) ?? []);
			currentDriveMap.set((msg.current_map as DriveMapId | null) ?? null);
			mapSwitching.set(false);
			lastError.set(null);
			vehicleList.set([]);
			spawnableObjects.set([]);
			scenarioList.set([]);
			requestVehicles();
			requestObjects();
			requestScenarios();
			break;

		case 'object_list':
			spawnableObjects.set((msg.objects as SpawnableObject[]) ?? []);
			break;

		case 'object_spawned':
			placedObjects.update(list => [...list, {
				actor_id: msg.actor_id as number,
				blueprint: msg.blueprint as string,
				pos: msg.pos as [number, number, number],
			}]);
			placedCount.set(msg.placed_count as number);
			break;

		case 'object_removed':
			placedObjects.update(list => list.slice(0, -1));
			placedCount.set(msg.placed_count as number);
			break;

		case 'undo_empty':
			// Nothing to undo — no state change
			break;

		case 'scenario_list':
			scenarioList.set((msg.scenarios as ScenarioInfo[]) ?? []);
			break;

		case 'scenario_saved':
			// Refresh scenario list after save
			requestScenarios();
			break;

		case 'scenario_loaded':
			placedCount.set((msg.placed_count as number) ?? 0);
			if (Array.isArray(msg.zones)) {
				v2xZones.set(msg.zones as V2xZone[]);
			}
			break;

		case 'scenario_deleted':
			requestScenarios();
			break;

		case 'xosc_list':
			xoscScenarioList.set((msg.scenarios as XoscScenarioInfo[]) ?? []);
			if (msg.status) {
				xoscRunnerStatus.set(msg.status as XoscRunnerStatus);
			}
			break;

		case 'xosc_started':
			xoscRunnerStatus.update(s => ({
				...s,
				running: true,
				file: (msg.file as string) ?? null,
				started_at: (msg.started_at as number) ?? Date.now() / 1000,
				exit_code: null,
			}));
			xoscEventLog.set([]);
			xoscLastResult.set(null);
			break;

		case 'xosc_event':
			xoscEventLog.update(log => {
				const next = [...log, { line: msg.line as string, ts: msg.ts as number }];
				return next.length > XOSC_LOG_MAX ? next.slice(-XOSC_LOG_MAX) : next;
			});
			break;

		case 'xosc_finished':
			xoscRunnerStatus.update(s => ({
				...s,
				running: false,
				exit_code: (msg.exit_code as number) ?? null,
			}));
			xoscLastResult.set({
				file: (msg.file as string) ?? null,
				exit_code: (msg.exit_code as number) ?? null,
				verdict: (msg.verdict as 'SUCCESS' | 'FAILURE') ?? 'FAILURE',
				duration_sec: (msg.duration_sec as number) ?? 0,
			});
			break;

		case 'xosc_stopped':
			xoscRunnerStatus.update(s => ({ ...s, running: false }));
			break;

		case 'camera_switched':
			// Acknowledged — no state change needed
			break;

		case 'camera_zoomed':
			if (typeof msg.altitude === 'number') {
				birdAltitude.set(msg.altitude);
			}
			break;

		case 'teleported':
			if (get(teleportStatus).state !== 'pending') {
				console.warn('[DriveWS] Ignoring unsolicited teleport acknowledgement');
				break;
			}
			if (typeof msg.request_id !== 'string' || msg.request_id !== activeTeleportRequestId) {
				console.warn('[DriveWS] Ignoring mismatched teleport acknowledgement');
				break;
			}
			if (!isTeleportedMessage(msg)) {
				setTeleportError('Bridge returned an invalid teleport acknowledgement.');
				break;
			}
			clearTeleportAckTimer();
			activeTeleportRequestId = null;
			teleportStatus.set({
				state: 'succeeded',
				message: `Teleported to (${msg.pos.map((coordinate) => coordinate.toFixed(1)).join(', ')})`,
				pos: msg.pos
			});
			telemetry.update((current) => ({ ...current, pos: msg.pos }));
			break;

		case 'teleport_error':
			if (get(teleportStatus).state !== 'pending') {
				console.warn('[DriveWS] Ignoring unsolicited teleport error');
				break;
			}
			if (typeof msg.request_id !== 'string' || msg.request_id !== activeTeleportRequestId) {
				console.warn('[DriveWS] Ignoring mismatched teleport error');
				break;
			}
			setTeleportError(
				isTeleportErrorMessage(msg) ? msg.message : 'Bridge returned an invalid teleport error.'
			);
			break;

		case 'v2x_signal_placed':
			v2xSignals.update(list => [...list, msg.signal as V2xSignal]);
			v2xSignalCount.set(msg.signal_count as number);
			break;

		case 'v2x_signal_removed':
			v2xSignals.update(list => list.filter(s => s.id !== (msg.signal_id as number)));
			v2xSignalCount.set(msg.signal_count as number);
			break;

		case 'v2x_undo_empty':
			break;

		case 'v2x_signal_list':
			v2xSignals.set((msg.signals as V2xSignal[]) ?? []);
			break;

		case 'trajectory_list':
			trajectoryList.set((msg.trajectories as TrajectoryInfo[]) ?? []);
			if (msg.status) {
				trajectoryStatus.set(msg.status as TrajectoryStatus);
			}
			break;

		case 'trajectory_started':
			trajectoryStatus.set({
				active: true,
				name: msg.name as string,
				duration: msg.duration as number,
				vehicle_id: msg.vehicle_id as number,
				elapsed: 0,
				finished: false,
			});
			break;

		case 'trajectory_stopped':
			trajectoryStatus.set({ active: false });
			break;

		case 'trajectory_status':
			trajectoryStatus.set(msg as unknown as TrajectoryStatus);
			break;

		case 'trajectory_uploaded':
			// Refresh list after upload so the new file appears in the dropdown
			requestTrajectories();
			break;

		case 'non_ego_vehicles_cleared':
			// Server side handles the destruction; UI updates via the next telemetry tick.
			break;

		case 'dynamic_actor_spawned':
			dynamicActors.update(list => {
				const actor = msg.actor;
				if (
					typeof actor !== 'object' ||
					actor === null ||
					typeof (actor as DynamicActor).actor_id !== 'number' ||
					typeof (actor as DynamicActor).blueprint !== 'string'
				) {
					return list;
				}
				const dynamicActor = actor as DynamicActor;
				return [...list.filter(a => a.actor_id !== dynamicActor.actor_id), dynamicActor];
			});
			break;

		case 'dynamic_actor_despawned':
		case 'dynamic_actor_missing':
			dynamicActors.update(list => list.filter(a => a.actor_id !== (msg.actor_id as number)));
			break;

		case 'dynamic_actors_despawned':
			dynamicActors.set([]);
			break;

		case 'error':
			mapSwitching.set(false);
			lastError.set(typeof msg.message === 'string' ? msg.message : 'Drive server error');
			if (get(sessionState) === 'reconstructing') {
				sessionState.set('error');
			}
			break;

		default:
			console.warn('[DriveWS] Unknown message type:', msg.type);
	}
}

// ── Actions ──

function send(msg: DriveMessage): boolean {
	if (ws && ws.readyState === WebSocket.OPEN) {
		try {
			const raw = JSON.stringify(msg);
			ws.send(raw);
			recordWire('TX', 'json', typeof msg.type === 'string' ? msg.type : '?', raw);
			return true;
		} catch {
			return false;
		}
	}
	return false;
}

export function requestVehicles(): void {
	send({ type: 'list_vehicles' });
}

export function requestMaps(): void {
	send({ type: 'list_maps' });
}

export function setDriveMap(map: DriveMapId): void {
	mapSwitching.set(true);
	lastError.set(null);
	send({ type: 'set_map', map });
}

export function startSession(start: string, end: string, vehicle?: string): void {
	resetTeleportStatus();
	sessionState.set('reconstructing');
	lastError.set(null);
	send({ type: 'start_session', start, end, vehicle: vehicle ?? 'vehicle.tesla.model3' });
}

export function sendControl(steer: number, throttle: number, brake: number, reverse: boolean = false): void {
	// Don't send control if not actively driving
	if (get(sessionState) !== 'driving') return;
	send({ type: 'control', s: steer, t: throttle, b: brake, rev: reverse });
}

export function switchCamera(view: CameraView): void {
	send({ type: 'camera_switch', view });
}

export function zoomCamera(factor: number): void {
	send({ type: 'camera_zoom', factor });
}

export function resetCameraZoom(): void {
	send({ type: 'camera_zoom', reset: true });
}

export function respawnVehicle(): void {
	send({ type: 'respawn' });
}

export function teleportVehicle(
	x: number,
	y: number,
	z?: number,
	yaw?: number
): boolean {
	if (get(teleportStatus).state === 'pending') {
		return false;
	}
	if (![x, y].every(Number.isFinite)) {
		setTeleportError('X and Y must be finite numbers.');
		return false;
	}
	if (Math.abs(x) > TELEPORT_COORD_ABS_LIMIT_M || Math.abs(y) > TELEPORT_COORD_ABS_LIMIT_M) {
		setTeleportError(`X and Y must be within ±${TELEPORT_COORD_ABS_LIMIT_M} metres.`);
		return false;
	}
	if (z !== undefined && !Number.isFinite(z)) {
		setTeleportError('Z must be a finite number or left blank.');
		return false;
	}
	if (z !== undefined && (z < TELEPORT_MIN_Z_M || z > TELEPORT_MAX_Z_M)) {
		setTeleportError(`Z must be between ${TELEPORT_MIN_Z_M} and ${TELEPORT_MAX_Z_M} metres.`);
		return false;
	}
	if (yaw !== undefined && !Number.isFinite(yaw)) {
		setTeleportError('Yaw must be a finite number or left blank.');
		return false;
	}
	if (yaw !== undefined && Math.abs(yaw) > TELEPORT_MAX_ABS_YAW_DEG) {
		setTeleportError(`Yaw must be within ±${TELEPORT_MAX_ABS_YAW_DEG} degrees.`);
		return false;
	}
	if (!get(driveConnected) || get(sessionState) !== 'driving') {
		setTeleportError('Start an active drive session before teleporting.');
		return false;
	}
	const requestId = newTeleportRequestId();
	const command: TeleportCommand = { type: 'teleport', request_id: requestId, x, y };
	if (z !== undefined) command.z = z;
	if (yaw !== undefined) command.yaw = yaw;

	activeTeleportRequestId = requestId;
	teleportStatus.set({ state: 'pending', message: 'Waiting for bridge acknowledgement…', pos: null });
	if (!send(command)) {
		setTeleportError('Drive WebSocket is not available.');
		return false;
	}

	clearTeleportAckTimer();
	teleportAckTimer = setTimeout(() => {
		if (
			get(teleportStatus).state === 'pending'
			&& activeTeleportRequestId === requestId
		) {
			setTeleportError('Teleport acknowledgement timed out.');
		}
	}, TELEPORT_ACK_TIMEOUT_MS);
	return true;
}

export function clearNonEgoVehicles(): void {
	send({ type: 'clear_non_ego_vehicles' });
}

export function requestObjects(): void {
	send({ type: 'list_objects' });
}

export function spawnObject(blueprint: string, offset: number = 8.0): void {
	send({ type: 'spawn_object', blueprint, offset });
}

export function spawnDynamicActor(
	blueprint: string,
	geofenceRadius: number = 35,
	message: string = ''
): void {
	const radius = Math.max(5, Math.min(250, Number.isFinite(geofenceRadius) ? geofenceRadius : 35));
	send({ type: 'spawn_dynamic_actor', blueprint, geofence_radius: radius, message });
}

export function despawnDynamicActor(actorId: number): void {
	send({ type: 'despawn_dynamic_actor', actor_id: actorId });
}

export function despawnDynamicActors(): void {
	send({ type: 'despawn_dynamic_actors' });
}

export function undoPlace(): void {
	send({ type: 'undo_place' });
}

export function requestScenarios(): void {
	send({ type: 'list_scenarios' });
}

export function saveScenario(name: string, zones: V2xZone[] = []): void {
	send({ type: 'save_scenario', name, zones });
}

export function loadScenario(file: string): void {
	send({ type: 'load_scenario', file });
}

export function deleteScenario(file: string): void {
	send({ type: 'delete_scenario', file });
}

// ── OpenSCENARIO (.xosc) Actions ──

export function requestXoscScenarios(): void {
	send({ type: 'list_xosc_scenarios' });
}

export function startXoscScenario(file: string): void {
	send({ type: 'start_xosc_scenario', file });
}

export function stopXoscScenario(): void {
	send({ type: 'stop_xosc_scenario' });
}

export function endSession(): void {
	if (get(teleportStatus).state === 'pending') {
		setTeleportError('Teleport cancelled because the drive session is ending.');
	}
	sessionState.set('ending');
	send({ type: 'end_session' });
}

// ── V2X Signal Actions ──

export function placeV2xSignal(message: string, signalType: string = 'warning', radius: number = 30.0): void {
	send({ type: 'place_v2x_signal', message, signal_type: signalType, radius });
}

export function removeV2xSignal(signalId: number): void {
	send({ type: 'remove_v2x_signal', signal_id: signalId });
}

export function undoV2xSignal(): void {
	send({ type: 'undo_v2x_signal' });
}

export function requestV2xSignals(): void {
	send({ type: 'list_v2x_signals' });
}

export function dismissV2xAlert(alertId: number): void {
	v2xAlerts.update(list => list.filter(a => a.id !== alertId));
}

// ── Weather Actions ──

export function setWeather(params: Record<string, number>): void {
	send({ type: 'set_weather', params });
}

export function setCameraSettings(params: Record<string, string | number>): void {
	send({ type: 'set_camera_settings', params });
}

// ── Traffic Actions ──

export function spawnTraffic(preset: string): void {
	send({ type: 'spawn_traffic', preset });
}

export function despawnTraffic(): void {
	send({ type: 'despawn_traffic' });
}

// ── V2X Zone Actions ──

export function syncV2xZones(zones: { polygon: [number, number][]; zone_kind?: string; signal_type: string; color: string }[]): void {
	send({ type: 'sync_v2x_zones', zones });
}

// ── Trajectory Actions ──

export function requestTrajectories(): void {
	send({ type: 'list_trajectories' });
}

export function uploadTrajectory(name: string, data: unknown[]): void {
	send({ type: 'upload_trajectory', name, data });
}

export function startTrajectory(file: string, vehicle?: string): void {
	send({ type: 'start_trajectory', file, vehicle: vehicle ?? 'vehicle.tesla.model3' });
}

export function stopTrajectory(): void {
	send({ type: 'stop_trajectory' });
}

export function requestTrajectoryStatus(): void {
	send({ type: 'trajectory_status' });
}
