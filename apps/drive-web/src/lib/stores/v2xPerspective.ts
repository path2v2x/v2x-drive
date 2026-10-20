/**
 * Per-vehicle V2X perspective: for every connected vehicle, the BSM it sends and the
 * BSMs it hears from the other vehicles inside the reception radius, with distances.
 * This is the view an onboard unit has: the latest state per sender, never a log.
 * The server pushes a snapshot every interval and the panel replaces its rows in place.
 *
 * Protocol (drive WebSocket; server side in v2x_viewer.py / v2x_perspective.py):
 *   -> v2x_perspective_subscribe {radius_m, interval_ms}   <- v2x_perspective {active, radius_m, vehicles[]}
 *   -> v2x_perspective_set {radius_m}                       <- v2x_perspective
 *   -> v2x_perspective_unsubscribe                          <- v2x_perspective {active: false}
 *      pushed every interval_ms while subscribed
 */

import { get, writable } from 'svelte/store';
import { registerDisconnectHook, registerDriveMessageHandler, sendDriveMessage } from './driveSocket';
import { formatHeading, formatSpeed, lightsLabel } from '$lib/v2xFormat';
import type { DriveMessage, V2xPerspectiveMessage, V2xPerspectiveTx } from '$lib/types';

export const DEFAULT_RADIUS_M = 300;
export const MIN_RADIUS_M = 10;
export const MAX_RADIUS_M = 5000;
export const INTERVAL_MS = 500;
/** Range choices offered in the panel (metres); DSRC reaches a few hundred metres. */
export const RADIUS_PRESETS = [50, 100, 200, 300, 500, 1000, 2000];
const RADIUS_STORAGE_KEY = 'v2x-drive:v2x-radius';

export function clampRadius(value: number): number {
	if (!Number.isFinite(value)) return DEFAULT_RADIUS_M;
	return Math.min(MAX_RADIUS_M, Math.max(MIN_RADIUS_M, Math.round(value)));
}

function storedRadius(): number {
	try {
		const raw = localStorage.getItem(RADIUS_STORAGE_KEY);
		return raw === null ? DEFAULT_RADIUS_M : clampRadius(Number(raw));
	} catch {
		return DEFAULT_RADIUS_M;
	}
}

export const v2xPerspective = writable<V2xPerspectiveMessage | null>(null);
export const v2xPerspectiveActive = writable<boolean>(false);
export const v2xRadiusM = writable<number>(typeof localStorage === 'undefined' ? DEFAULT_RADIUS_M : storedRadius());
/** Vehicle keys whose "sends / hears" details are open. */
export const v2xExpanded = writable<Set<string>>(new Set());

registerDriveMessageHandler('v2x_perspective', (msg: DriveMessage) => {
	const m = msg as unknown as V2xPerspectiveMessage;
	if (m.active === false) {
		v2xPerspectiveActive.set(false);
		v2xPerspective.set(null);
		return;
	}
	v2xPerspectiveActive.set(true);
	v2xPerspective.set({ ...m, vehicles: Array.isArray(m.vehicles) ? m.vehicles : [] });
	if (typeof m.radius_m === 'number') v2xRadiusM.set(clampRadius(m.radius_m));
});

registerDisconnectHook(() => {
	v2xPerspectiveActive.set(false);
	v2xPerspective.set(null);
});

export function subscribeV2xPerspective(): boolean {
	return sendDriveMessage({ type: 'v2x_perspective_subscribe', radius_m: get(v2xRadiusM), interval_ms: INTERVAL_MS });
}

export function unsubscribeV2xPerspective(): boolean {
	return sendDriveMessage({ type: 'v2x_perspective_unsubscribe' });
}

/** Change the reception radius (metres); remembered per browser. */
export function setV2xRadius(meters: number): boolean {
	const radius = clampRadius(meters);
	v2xRadiusM.set(radius);
	try {
		localStorage.setItem(RADIUS_STORAGE_KEY, String(radius));
	} catch {
		// private window or storage blocked: the radius just does not persist
	}
	return sendDriveMessage({ type: 'v2x_perspective_set', radius_m: radius });
}

export function toggleV2xExpanded(key: string): void {
	v2xExpanded.update((open) => {
		const next = new Set(open);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		return next;
	});
}

export function resetV2xPerspective(): void {
	v2xPerspective.set(null);
	v2xPerspectiveActive.set(false);
	v2xExpanded.set(new Set());
}

// ── Display helpers (pure, unit tested) ──

/** 'ahead' | 'behind' | 'left' | 'right' from a relative bearing in CARLA's frame (positive = right). */
export function relBearingLabel(deg: number | null | undefined): string {
	if (typeof deg !== 'number' || !Number.isFinite(deg)) return '';
	const a = Math.abs(deg);
	if (a <= 30) return 'ahead';
	if (a >= 150) return 'behind';
	return deg > 0 ? 'right' : 'left';
}

export function formatDistance(meters: number | null | undefined): string {
	if (typeof meters !== 'number' || !Number.isFinite(meters)) return '–';
	if (meters >= 1000) return `${(meters / 1000).toFixed(1)} km`;
	return `${Math.round(meters)} m`;
}

/** One plain line for what a vehicle transmits: speed and heading from its own BSM, plus the
 * siren and lights when they are in use. */
export function txLine(tx: V2xPerspectiveTx | null | undefined): string {
	if (!tx) return 'nothing';
	if (!tx.summary) return `${tx.msg_type} (not decoded)`;
	const parts = [formatSpeed(tx.summary.speed_mps, 0), formatHeading(tx.summary.heading_deg, 0)];
	const lights = lightsLabel(tx.summary);
	if (lights && lights !== 'off') parts.push(lights);
	if (tx.summary.brakes) parts.push('braking');
	return parts.join(' · ');
}

export function formatAge(seconds: number | null | undefined): string {
	if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '';
	return seconds < 10 ? `${seconds.toFixed(1)} s ago` : `${Math.round(seconds)} s ago`;
}
