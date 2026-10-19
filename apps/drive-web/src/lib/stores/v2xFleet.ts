/**
 * V2X fleet store — the server-owned civilians and emergency vehicles that
 * react to V2X messages (apps/drive-server/digital_twin_bridge/v2x_behaviors).
 *
 * Protocol:
 *   -> spawn_v2x_vehicle {kind, profile?, blueprint?, spawn, destination?, params?}  <- v2x_vehicle_spawned
 *   -> despawn_v2x_vehicle {actor_id}        <- v2x_vehicle_despawned
 *   -> despawn_v2x_vehicles                  <- v2x_vehicles_despawned
 *   -> list_v2x_vehicles                     <- v2x_vehicles {vehicles, tracks, stats}
 *   -> set_erv_state {actor_id, siren?, lights?, arrived?, manual?, destination?}  <- v2x_vehicle_state
 *                                            <- v2x_vehicle_event (pushed to every client)
 */

import { get, writable } from 'svelte/store';
import {
	lastError,
	registerDisconnectHook,
	registerDriveMessageHandler,
	sendDriveMessage,
	telemetry
} from './driveSocket';
import type {
	DriveMessage,
	ErvTrack,
	V2xFleetEvent,
	V2xFleetStats,
	V2xFleetVehicle,
	V2xSpawnSpec
} from '$lib/types';

export const EVENTS_MAX = 100;

export const fleetVehicles = writable<V2xFleetVehicle[]>([]);
export const fleetTracks = writable<ErvTrack[]>([]);
export const fleetStats = writable<V2xFleetStats | null>(null);
export const fleetEvents = writable<V2xFleetEvent[]>([]);
export const fleetListedAt = writable<number>(0);

function isVehicle(value: unknown): value is V2xFleetVehicle {
	return typeof value === 'object' && value !== null
		&& typeof (value as V2xFleetVehicle).actor_id === 'number'
		&& typeof (value as V2xFleetVehicle).kind === 'string';
}

function upsert(vehicle: V2xFleetVehicle): void {
	fleetVehicles.update((list) => {
		const idx = list.findIndex((v) => v.actor_id === vehicle.actor_id);
		if (idx === -1) return [...list, vehicle];
		const next = list.slice();
		next[idx] = { ...list[idx], ...vehicle };
		return next;
	});
}

function onVehicles(msg: DriveMessage): void {
	fleetVehicles.set(Array.isArray(msg.vehicles) ? (msg.vehicles as V2xFleetVehicle[]).filter(isVehicle) : []);
	fleetTracks.set(Array.isArray(msg.tracks) ? (msg.tracks as ErvTrack[]) : []);
	fleetStats.set(msg.stats && typeof msg.stats === 'object' ? (msg.stats as V2xFleetStats) : null);
	fleetListedAt.set(Date.now());
}

function onSpawnedOrState(msg: DriveMessage): void {
	if (isVehicle(msg.vehicle)) upsert(msg.vehicle);
}

function onDespawned(msg: DriveMessage): void {
	const id = msg.actor_id;
	if (typeof id !== 'number') return;
	fleetVehicles.update((list) => list.filter((v) => v.actor_id !== id));
}

function onAllDespawned(): void {
	fleetVehicles.set([]);
}

function onEvent(msg: DriveMessage): void {
	const ev = msg as V2xFleetEvent;
	if (typeof ev.actor_id !== 'number') return;
	fleetEvents.update((list) => {
		const next = [...list, ev];
		return next.length > EVENTS_MAX ? next.slice(-EVENTS_MAX) : next;
	});
	if (ev.to === null) {
		fleetVehicles.update((list) => list.filter((v) => v.actor_id !== ev.actor_id));
		return;
	}
	fleetVehicles.update((list) =>
		list.map((v) =>
			v.actor_id === ev.actor_id && typeof ev.to === 'string'
				? { ...v, state: ev.to, reason: ev.reason ?? v.reason, since_s: 0 }
				: v
		)
	);
}

registerDriveMessageHandler('v2x_vehicles', onVehicles);
registerDriveMessageHandler('v2x_vehicle_spawned', onSpawnedOrState);
registerDriveMessageHandler('v2x_vehicle_state', onSpawnedOrState);
registerDriveMessageHandler('v2x_vehicle_despawned', onDespawned);
registerDriveMessageHandler('v2x_vehicles_despawned', onAllDespawned);
registerDriveMessageHandler('v2x_vehicle_event', onEvent);
registerDisconnectHook(resetFleetStore);

// Positions and states ride along with the ego's telemetry at 20 Hz.
telemetry.subscribe((t) => {
	const compact = t.v2x_fleet;
	if (!Array.isArray(compact) || compact.length === 0) return;
	fleetVehicles.update((list) => {
		if (list.length === 0) return list;
		let changed = false;
		const next = list.map((v) => {
			const c = compact.find((x) => x.actor_id === v.actor_id);
			if (!c) return v;
			changed = true;
			return { ...v, pos: c.pos ?? v.pos, yaw: c.yaw ?? v.yaw, speed_mps: c.speed_mps ?? v.speed_mps, state: c.state ?? v.state };
		});
		return changed ? next : list;
	});
});

// ── Actions ──

export function requestFleet(): boolean {
	return sendDriveMessage({ type: 'list_v2x_vehicles' });
}

export function spawnFleetVehicle(
	kind: 'civilian' | 'erv',
	spawn: V2xSpawnSpec,
	options: {
		profile?: string;
		blueprint?: string;
		destination?: 'random' | 'none' | { x: number; y: number };
		params?: Record<string, unknown>;
		/** VOICES scenario slot (e.g. PATH-M-1) the car publishes under; a car parked under it is adopted */
		role?: string;
	} = {}
): boolean {
	const msg: DriveMessage = { type: 'spawn_v2x_vehicle', kind, spawn };
	if (options.profile) msg.profile = options.profile;
	if (options.blueprint) msg.blueprint = options.blueprint;
	if (options.destination !== undefined) msg.destination = options.destination;
	if (options.params) msg.params = options.params;
	if (options.role) msg.role = options.role;
	return sendDriveMessage(msg);
}

/** Spawn ``count`` civilians in the ego's lane, ``spacingM`` apart, the first ``aheadM`` ahead. */
export function spawnCiviliansAhead(count: number, aheadM = 60, spacingM = 25, profile = 'civilian'): number {
	let sent = 0;
	const n = Math.max(1, Math.min(8, Math.floor(count)));
	for (let i = 0; i < n; i++) {
		if (spawnFleetVehicle('civilian', { ahead_of: 'ego', distance_m: aheadM + i * spacingM }, { profile })) sent += 1;
	}
	return sent;
}

export function spawnErv(
	profile: 'ambulance' | 'fire' | 'police',
	placement: 'behind' | 'random',
	distanceM = 150,
	role = ''
): boolean {
	const spawn: V2xSpawnSpec = placement === 'behind' ? { behind: 'ego', distance_m: distanceM } : 'random';
	return spawnFleetVehicle('erv', spawn, { profile, destination: 'random', role: role || undefined });
}

/** Remove a vehicle. A slot car is parked and kept; ``release`` destroys it anyway. */
export function despawnFleetVehicle(actorId: number, release = false): boolean {
	const msg: DriveMessage = { type: 'despawn_v2x_vehicle', actor_id: actorId };
	if (release) msg.release = true;
	return sendDriveMessage(msg);
}

export function despawnFleet(release = false): boolean {
	const msg: DriveMessage = { type: 'despawn_v2x_vehicles' };
	if (release) msg.release = true;
	return sendDriveMessage(msg);
}

export function setErvState(
	actorId: number,
	patch: { siren?: boolean; lights?: boolean; arrived?: boolean; manual?: boolean; response_type?: string; destination?: 'random' | { x: number; y: number } }
): boolean {
	return sendDriveMessage({ type: 'set_erv_state', actor_id: actorId, ...patch });
}

export function clearFleetEvents(): void {
	fleetEvents.set([]);
}

export function resetFleetStore(): void {
	fleetVehicles.set([]);
	fleetTracks.set([]);
	fleetStats.set(null);
	fleetEvents.set([]);
	fleetListedAt.set(0);
}

/** A readable label for a state, grouped by what the car is doing. */
export function stateTone(state: string): 'calm' | 'alert' | 'yield' | 'stopped' | 'erv' | 'done' {
	switch (state) {
		case 'PARKED':
			return 'done';
		case 'NORMAL':
		case 'EN_ROUTE':
			return 'calm';
		case 'MONITOR':
		case 'YIELD_PREP':
		case 'APPROACH_JUNCTION':
		case 'FOLLOW_SLOW':
		case 'WAIT_FOR_YIELD':
			return 'alert';
		case 'PULL_OVER':
		case 'MERGE_BACK':
		case 'SLOW_IN_LANE':
		case 'MOVE_OVER':
		case 'PASS_STOPPED_ERV':
		case 'OPPOSING_PASS':
		case 'PASS_LEFT_IN_LANE':
		case 'OPPOSING_LANE_PASS':
		case 'CROSSING':
			return 'yield';
		case 'STOPPED_HOLD':
		case 'WAIT_CLEAR':
		case 'WAIT_BEHIND':
		case 'STOP_AT_CONTROL':
		case 'NO_PROGRESS':
		case 'MANUAL':
			return 'stopped';
		case 'ARRIVED':
			return 'done';
		default:
			return 'erv';
	}
}

export function fleetErrorIfAny(): string | null {
	return get(lastError);
}
