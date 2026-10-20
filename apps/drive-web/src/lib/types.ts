export interface TrackedObject {
	object_id: string;
	object_type: 'traffic_cone' | 'vehicle' | 'walker' | string;
	lat: number;
	lon: number;
	confidence: number;
	street_name: string;
	timestamp_utc: string;
	snapshot_url: string | null;
	snapshot_timestamp: string | null;
	last_updated: number; // unix ms
}

export interface BridgeStatus {
	status: 'connected' | 'disconnected' | 'stale' | 'error';
	carla_fps: number;
	objects_tracked: number;
	cameras_active: number;
	/** Producer timestamp from bridge_status.last_heartbeat, normalized to ISO-8601. */
	last_heartbeat: string | null;
	/** Producer timestamp for the state snapshot itself. */
	updated_at: string | null;
}

export interface SnapshotHistoryEntry {
	url: string;
	timestamp: string;
	object_id: string;
}

// ── Twin server detection history (72 h SQLite on path-rfs) ──

/** One raw per-camera detection from `GET /detections/history`. */
export interface DetectionRecord {
	ts: string;
	camera: string;
	object_id: string;
	object_type: string;
	confidence: number;
	lat: number;
	lon: number;
}

export interface DetectionHistoryPage {
	items: DetectionRecord[];
	next: string | null;
}

/** Per-object summary from `GET /detections/objects`. */
export interface DetectionObject {
	object_id: string;
	object_type: string;
	first_seen: string;
	last_seen: string;
	count: number;
	max_confidence: number;
	cameras: string[];
	last_lat: number;
	last_lon: number;
}

export interface CoverageBucket {
	start: string;
	detections: number;
	objects: number;
}

export interface DetectionCoverage {
	start: string;
	end: string;
	bucket_seconds: number;
	buckets: CoverageBucket[];
}

export interface DemoVideo {
	fileName: string;
	title: string;
	url: string;
	posterUrl: string | null;
	sizeBytes: number;
	lastModified: string | null;
}

export type FreshnessLevel = 'fresh' | 'stale' | 'old';

// ── Drive Mode Types ──

export type CameraView = 'chase' | 'hood' | 'bird' | 'free';

export type DriveSessionState =
	| 'idle'
	| 'connecting'
	| 'reconstructing'
	| 'ready'
	| 'driving'
	| 'ending'
	| 'error';

export interface NearbyActor {
	id: number;
	pos: [number, number];
	yaw: number;
	type: 'traffic' | 'dynamic' | 'civilian' | 'erv' | 'other';
}

/** One V2X fleet vehicle as carried in telemetry (20 Hz), see V2xFleetVehicle for the full record. */
export interface V2xFleetCompact {
	actor_id: number;
	kind: 'civilian' | 'erv';
	name: string;
	state: string;
	pos: [number, number] | null;
	yaw: number | null;
	speed_mps: number | null;
}

export interface DynamicActor {
	actor_id: number;
	blueprint: string;
	name: string;
	pos: [number, number, number];
	yaw: number;
	geofence_radius: number;
	message: string;
	autopilot: boolean;
}

export interface ActorGeofenceAlert {
	actor: DynamicActor;
	distance: number;
}

/** Driving HUD block (manual_control.py's line order); every field may be missing on old servers. */
export interface HudTelemetry {
	server_fps: number;
	frame: number | null;
	sim_time: number | null;
	speed_limit: number | null;
	compass: number;
	accel: [number, number, number];
	gyro: [number, number, number];
	imu: boolean;
	gnss: [number, number] | null;
	height: number;
	hand_brake: boolean;
	manual: boolean;
	collision: { intensity: number; with: string; count: number } | null;
	vehicles: number;
	vehicle: string;
	map: string;
}

export interface VehicleTelemetry {
	speed: number;
	gear: number;
	pos: [number, number, number];
	rot: [number, number, number];
	steer: number;
	throttle: number;
	brake: number;
	nearby_actors?: NearbyActor[];
	dynamic_actors?: DynamicActor[];
	v2x_fleet?: V2xFleetCompact[];
	hud?: HudTelemetry;
}

// ── Editor mode ──

export type SessionMode = 'driving' | 'editor';

/** The editor camera's pose, replied to every camera message. */
export interface CameraPoseMessage extends DriveMessage {
	type: 'camera_pose';
	x: number;
	y: number;
	z: number;
	yaw: number;
	pitch: number;
	speed: number;
	following: number | null;
}

export interface EditorStartPose {
	x?: number;
	y?: number;
	z?: number;
	yaw?: number;
	pitch?: number;
}

export type WorldActorGroup =
	| 'ego'
	| 'voices'
	| 'ghost'
	| 'fleet'
	| 'traffic'
	| 'dynamic'
	| 'trajectory'
	| 'foreign'
	| 'walker'
	| 'prop'
	| 'sensor'
	| 'light'
	| 'other';

/** One row of the editor's outliner. */
export interface WorldActor {
	id: number;
	type_id: string;
	name: string;
	role: string;
	group: WorldActorGroup;
	owner: string;
	pos: [number, number, number] | null;
	yaw: number | null;
	speed_mps: number | null;
	parent: number | null;
	alive: boolean;
}

export interface WorldActorsMessage extends DriveMessage {
	type: 'world_actors';
	actors: WorldActor[];
	total: number;
	truncated: boolean;
	groups: Record<string, number>;
	following: number | null;
}

export interface ActorDetails extends WorldActor {
	attributes: Record<string, string>;
	bbox: [number, number, number] | null;
	velocity: [number, number, number] | null;
	rot: [number, number, number] | null;
	control?: {
		throttle: number;
		steer: number;
		brake: number;
		hand_brake: boolean;
		reverse: boolean;
		gear: number;
		manual_gear_shift: boolean;
	} | null;
	lights?: string[] | null;
	speed_limit?: number | null;
	parent_type: string | null;
}

/** One button of a driving-view control bar (see ControlBar.svelte). */
export interface RailItem {
	id: string;
	label: string;
	title?: string;
	/** SVG path data (24x24 viewBox, stroked); a short text badge is used when empty */
	icon?: string;
	text?: string;
	active?: boolean;
	tone?: 'default' | 'info' | 'warn' | 'danger' | 'ok';
	onClick: () => void;
}

export type TrafficPreset = 'none' | 'light' | 'medium' | 'heavy' | 'chaos';

export interface GamepadCalibration {
	steerAxis: number;
	gasAxis: number;
	brakeAxis: number;
	steerInverted: boolean;
	gasInverted: boolean;
	brakeInverted: boolean;
}

export interface VehicleOption {
	id: string;
	name: string;
	wheels: number;
}

export type DriveMapId = 'richmond' | 'san_ramon';

export interface DriveMapOption {
	id: DriveMapId;
	label: string;
	map_name: string;
}

export interface SpawnableObject {
	id: string;
	name: string;
	category: 'vehicle' | 'prop';
}

export interface PlacedObject {
	actor_id: number;
	blueprint: string;
	pos: [number, number, number];
}

export interface ScenarioInfo {
	name: string;
	file: string;
	object_count: number;
	zone_count?: number;
}

export interface V2xSignal {
	id: number;
	pos: [number, number, number];
	message: string;
	signal_type: 'warning' | 'info' | 'alert';
	radius: number;
}

export interface V2xAlert {
	id: number;
	message: string;
	signal_type: 'warning' | 'info' | 'alert';
	distance: number;
}

export type V2xZoneKind = 'warning' | 'geofence';

export interface V2xZone {
	id: string;
	name: string;
	message: string;
	zone_kind: V2xZoneKind;
	signal_type: 'warning' | 'info' | 'alert';
	polygon: [number, number][];
	color: string;
}

export interface DriveMessage {
	type: string;
	[key: string]: unknown;
}

/** Client-to-bridge teleport request. Optional values are omitted, never sent as null. */
export interface TeleportCommand extends DriveMessage {
	type: 'teleport';
	request_id: string;
	x: number;
	y: number;
	z?: number;
	yaw?: number;
}

/** Bridge acknowledgement after CARLA reports the vehicle's final position. */
export interface TeleportedMessage extends DriveMessage {
	type: 'teleported';
	request_id: string;
	success: true;
	pos: [number, number, number];
	yaw?: number;
	snapped_to_road?: boolean;
}

/** Validation/runtime failure returned specifically for a teleport request. */
export interface TeleportErrorMessage extends DriveMessage {
	type: 'teleport_error';
	request_id: string;
	success: false;
	message: string;
}

export type TeleportRequestState = 'idle' | 'pending' | 'succeeded' | 'error';

export interface TeleportStatus {
	state: TeleportRequestState;
	message: string | null;
	pos: [number, number, number] | null;
}

export interface TrajectoryInfo {
	file: string;
	samples: number;
}

export interface TrajectoryStatus {
	active: boolean;
	name?: string;
	elapsed?: number;
	duration?: number;
	vehicle_id?: number;
	finished?: boolean;
}

export interface XoscScenarioInfo {
	file: string;
	name: string;
	size_bytes: number;
}

export interface XoscRunnerStatus {
	running: boolean;
	file?: string | null;
	started_at?: number | null;
	exit_code?: number | null;
	scenario_runner_configured: boolean;
}

export interface XoscEvent {
	line: string;
	ts: number;
}

export interface XoscFinishedEvent {
	file: string | null;
	exit_code: number | null;
	verdict: 'SUCCESS' | 'FAILURE';
	duration_sec: number;
}

// ── V2X message viewer (per-vehicle J2735 feed from the DT / TENA stack) ──

export type V2xVehicleKind = 'voices' | 'ghost' | 'drive' | 'other';
export type V2xFeedSource = 'tena' | 'udp' | 'stale' | 'none';

export interface V2xVehicleFeed {
	source: V2xFeedSource;
	last_rx: number | null;
	age_s: number | null;
	rate_hz: number;
	count: number;
	types?: Record<string, number>;
}

export interface V2xVehicleEntry {
	key: string;
	actor_id: number | null;
	role: string;
	type_id: string;
	kind: V2xVehicleKind;
	subkind: string;
	org: string;
	bsm_id: string;
	pos: [number, number, number] | null;
	yaw: number | null;
	speed_mps: number | null;
	emergency: boolean;
	special_lights: boolean | null;
	feed: V2xVehicleFeed;
	in_world: boolean;
}

export interface V2xFeedStatus {
	decoder: { available: boolean; error: string | null };
	json_stream: {
		enabled?: boolean;
		listening?: string | null;
		connected?: boolean;
		peer?: string | null;
		records?: number;
		records_per_s?: number;
		parse_errors?: number;
		connections?: number;
	};
	udp: { enabled?: boolean; bind?: string; datagrams?: number; datagrams_per_s?: number };
	roster: { file?: string | null; vehicles?: number; live_vehicles?: number; local_org?: string; scenario?: string };
	hub: { total?: number; senders?: number; subscriptions?: number };
	keeper: { launches: number; last_check: Record<string, unknown> | null } | null;
}

export interface V2xVehicleListMessage extends DriveMessage {
	type: 'v2x_vehicle_list';
	version: number;
	ts: number;
	feed: V2xFeedStatus;
	vehicles: V2xVehicleEntry[];
}

export interface V2xBsmLights {
	siren: string | null;
	lights: string | null;
	multi: string | null;
	role: string | null;
	response_type: string | null;
	events: unknown;
}

/** Flattened BSM core data in SI units (see j2735_codec.summarize_bsm). */
export interface V2xBsmSummary {
	id: string | null;
	msg_cnt: number | null;
	sec_mark: number | null;
	lat: number | null;
	lon: number | null;
	elev_m: number | null;
	speed_mps: number | null;
	heading_deg: number | null;
	brakes: boolean | null;
	transmission: string | null;
	size_m: { length: number | null; width: number | null } | null;
	lights: V2xBsmLights | null;
	coords: 'wgs84' | 'origin-relative' | 'unknown';
	emergency?: boolean;
	classification?: number;
}

export interface V2xMessageRow {
	key: string;
	seq: number;
	t_rx: number;
	msg_type: string;
	source: 'tena' | 'udp';
	sender: string | null;
	uuid: string | null;
	raw_hex: string;
	size: number;
	summary: V2xBsmSummary | Record<string, unknown> | null;
	error: string | null;
	tx: string | null;
	decoded?: unknown;
}

export interface V2xMessagesMessage extends DriveMessage {
	type: 'v2x_messages';
	dropped: number;
	batch: V2xMessageRow[];
}

export interface V2xSubscribedMessage extends DriveMessage {
	type: 'v2x_subscribed';
	keys: string[];
	detail: 'compact' | 'full';
	unknown?: string[];
}

export interface V2xDecodedMessage extends DriveMessage {
	type: 'v2x_decoded';
	raw_hex: string;
	msg_type: string;
	decoded: unknown;
	summary?: unknown;
	error: string | null;
}

// ── V2X-reactive driving models (server-owned fleet of civilians and ERVs) ──

export type V2xSpawnSpec =
	| 'random'
	| { ahead_of: 'ego' | number; distance_m: number }
	| { behind: 'ego' | number; distance_m: number }
	| { spawn_index: number }
	| { x: number; y: number; z?: number; yaw?: number }
	| { lat: number; lon: number; yaw?: number };

export interface V2xFleetVehicle {
	actor_id: number;
	name: string;
	kind: 'civilian' | 'erv';
	profile: string;
	blueprint: string;
	state: string;
	reason: string;
	since_s: number;
	pos: [number, number] | null;
	yaw: number | null;
	speed_mps: number | null;
	target_speed_mps: number | null;
	offset_m: number;
	stop: boolean;
	destination: [number, number] | null;
	dist_to_destination_m: number | null;
	arrived: boolean;
	lane_change: string | null;
	passing: boolean;
	spawned_at: number;
	/** VOICES scenario slot the car publishes under ('' = none) and whether it is parked under it */
	slot?: string;
	parked?: boolean;
	/** ERV only */
	siren?: boolean;
	lights?: boolean | string[];
	response_type?: string;
	manual?: boolean;
	doors_open?: boolean;
	/** civilian only */
	erv_key?: string | null;
}

export interface ErvTrack {
	key: string;
	actor_id: number | null;
	x: number | null;
	y: number | null;
	yaw: number | null;
	speed_mps: number;
	siren: boolean;
	lights: boolean;
	active: boolean;
	stationary: boolean;
	role: string;
	response_type: string | null;
	source: string;
	age_s: number;
	fleet_state: string | null;
}

export type VoicesSlotState = 'free' | 'kept' | 'session' | 'fleet' | 'taken';

/** A scenario name of our organisation the ego or a fleet ERV may publish under. */
export interface VoicesSlot {
	name: string;
	vehicle_type: string;
	bsm_id: string;
	emergency: boolean;
	state: VoicesSlotState;
	holder: string;
	actor_id: number | null;
}

export interface VoicesSlotsMessage extends DriveMessage {
	type: 'voices_slots';
	slots: VoicesSlot[];
	local_org: string;
	default: string;
	available: boolean;
	scenario: string;
}

export interface V2xFleetStats {
	count: number;
	parked?: number;
	max: number;
	tick_ms_avg: number;
	tick_ms_p95: number;
	budget_ms: number;
	degraded: boolean;
	decision_hz: number;
	tracks: number;
	events: number;
}

export interface V2xFleetEvent extends DriveMessage {
	type: 'v2x_vehicle_event';
	actor_id: number;
	name: string;
	kind: string;
	from: string | null;
	to: string | null;
	reason: string;
	t: number;
}

/** Background-traffic script control (the SUMO bridge on RFS), polled by the editor. */
export interface BgTrafficStatus extends DriveMessage {
	type: 'bg_traffic';
	available: boolean;
	label: string;
	state: 'unavailable' | 'stopped' | 'starting' | 'running' | 'stopping';
	running: boolean;
	since?: number | null;
	uptime_s?: number | null;
	last_start?: { at: number; ok: boolean; code: number; output: string } | null;
	last_stop?: { at: number; ok: boolean; code: number; output: string } | null;
	last_line?: string;
	error?: string;
}

/** What one connected vehicle transmits: its latest BSM as a receiver would see it. */
export interface V2xPerspectiveTx {
	msg_type: string;
	t_rx: number;
	age_s: number;
	rate_hz: number;
	count: number;
	source: V2xFeedSource;
	size: number;
	summary: V2xBsmSummary | null;
	error: string | null;
}

/** One sender a vehicle hears inside its reception radius. */
export interface V2xPerspectiveRx {
	key: string;
	name: string;
	actor_id: number | null;
	kind: V2xVehicleKind;
	org: string;
	emergency: boolean;
	distance_m: number;
	bearing_deg: number | null;
	rel_bearing_deg: number | null;
	msg_type: string;
	age_s: number | null;
	rate_hz: number;
	source: V2xFeedSource;
	summary: V2xBsmSummary | null;
}

export interface V2xPerspectiveVehicle {
	key: string;
	actor_id: number | null;
	name: string;
	kind: V2xVehicleKind;
	subkind: string;
	org: string;
	emergency: boolean;
	pos: number[] | null;
	yaw: number | null;
	speed_mps: number | null;
	tx: V2xPerspectiveTx | null;
	rx: V2xPerspectiveRx[];
	rx_count: number;
}

/** The per-vehicle V2X view, pushed every interval_ms while subscribed (v2x_perspective.py). */
export interface V2xPerspectiveMessage extends DriveMessage {
	type: 'v2x_perspective';
	active: boolean;
	ts?: number;
	radius_m: number | null;
	interval_ms?: number;
	vehicles: V2xPerspectiveVehicle[];
	count: number;
	transmitting?: number;
}
