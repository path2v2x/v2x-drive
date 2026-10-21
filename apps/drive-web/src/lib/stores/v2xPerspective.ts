/**
 * Per-vehicle V2X perspective: for every connected vehicle, the BSM it sends and the
 * BSMs it hears from the other vehicles inside the reception radius, with distances.
 * This is the view an onboard unit has: the latest state per sender, never a log.
 * The server pushes a snapshot every interval and the panel replaces its rows in place.
 *
 * Protocol (drive WebSocket; server side in v2x_viewer.py / v2x_perspective.py):
 *   -> v2x_perspective_subscribe {radius_m, interval_ms, detail}   <- v2x_perspective {active, radius_m, detail, vehicles[]}
 *   -> v2x_perspective_set {radius_m?, detail?}                     <- v2x_perspective  (detail 'full' adds the decoded body)
 *   -> v2x_perspective_unsubscribe                          <- v2x_perspective {active: false}
 *      pushed every interval_ms while subscribed
 */

import { get, writable } from 'svelte/store';
import { registerDisconnectHook, registerDriveMessageHandler, sendDriveMessage } from './driveSocket';
import { formatHeading, formatSpeed, lightsLabel } from '$lib/v2xFormat';
import type { DriveMessage, V2xBsmSummary, V2xPerspectiveMessage, V2xPerspectiveTx } from '$lib/types';

export const DEFAULT_RADIUS_M = 300;
export const MIN_RADIUS_M = 10;
export const MAX_RADIUS_M = 5000;
export const INTERVAL_MS = 500;
/** Range choices offered in the panel (metres); DSRC reaches a few hundred metres. */
export const RADIUS_PRESETS = [50, 100, 200, 300, 500, 1000, 2000];
const RADIUS_STORAGE_KEY = 'v2x-drive:v2x-radius';
const DETAIL_STORAGE_KEY = 'v2x-drive:v2x-detail';

export type V2xDetailLevel = 'simple' | 'full';

function storedDetail(): V2xDetailLevel {
	try {
		return localStorage.getItem(DETAIL_STORAGE_KEY) === 'full' ? 'full' : 'simple';
	} catch {
		return 'simple';
	}
}

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
/** 'simple' (speed, heading, distances) or 'full' (every BSM field and the raw body). */
export const v2xDetail = writable<V2xDetailLevel>(typeof localStorage === 'undefined' ? 'simple' : storedDetail());

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
	if (m.detail === 'full' || m.detail === 'simple') v2xDetail.set(m.detail);
});

registerDisconnectHook(() => {
	v2xPerspectiveActive.set(false);
	v2xPerspective.set(null);
});

export function subscribeV2xPerspective(): boolean {
	return sendDriveMessage({
		type: 'v2x_perspective_subscribe',
		radius_m: get(v2xRadiusM),
		interval_ms: INTERVAL_MS,
		detail: get(v2xDetail)
	});
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

/** Switch between the simple and the full view; remembered per browser. */
export function setV2xDetail(level: V2xDetailLevel): boolean {
	v2xDetail.set(level);
	try {
		localStorage.setItem(DETAIL_STORAGE_KEY, level);
	} catch {
		// storage blocked: the choice just does not persist
	}
	return sendDriveMessage({ type: 'v2x_perspective_set', detail: level });
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

// ── Full view helpers (pure, unit tested) ──

/** What the full view reads: a vehicle's tx block or one heard row (both fit). */
export interface V2xFieldSource {
	summary: V2xBsmSummary | null;
	msg_type?: string;
	rate_hz?: number;
	age_s?: number | null;
	source?: string;
	size?: number;
	decoded?: Record<string, unknown> | null;
}

function show(value: unknown): string {
	if (value === null || value === undefined) return '–';
	if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
	if (typeof value === 'boolean') return value ? 'yes' : 'no';
	return String(value);
}

/** Every decoded BSM field as [label, value] rows, for the full view. */
export function summaryFields(entry: V2xFieldSource): [string, string][] {
	const rows: [string, string][] = [];
	if (entry.msg_type) rows.push(['message', `${entry.msg_type}${typeof entry.size === 'number' ? ` · ${entry.size} B` : ''}`]);
	if (typeof entry.rate_hz === 'number') rows.push(['rate', `${entry.rate_hz.toFixed(1)} Hz`]);
	if (typeof entry.age_s === 'number') rows.push(['received', `${formatAge(entry.age_s)}${entry.source ? ` · ${entry.source}` : ''}`]);
	const s = entry.summary;
	if (!s) return rows;
	rows.push(['id', show(s.id)]);
	rows.push(['msgCnt', show(s.msg_cnt)]);
	rows.push(['secMark', typeof s.sec_mark === 'number' ? `${s.sec_mark} ms` : '–']);
	rows.push(['latitude', show(s.lat)]);
	rows.push(['longitude', show(s.lon)]);
	rows.push(['coords', s.coords]);
	rows.push(['elevation', typeof s.elev_m === 'number' ? `${s.elev_m.toFixed(1)} m` : '–']);
	rows.push(['speed', formatSpeed(s.speed_mps)]);
	rows.push(['heading', formatHeading(s.heading_deg)]);
	rows.push(['brakes', show(s.brakes)]);
	rows.push(['transmission', show(s.transmission)]);
	rows.push(['size', s.size_m ? `${show(s.size_m.length)} × ${show(s.size_m.width)} m` : '–']);
	if (s.lights) {
		rows.push(['siren', show(s.lights.siren)]);
		rows.push(['lightbar', show(s.lights.lights)]);
		rows.push(['multi', show(s.lights.multi)]);
		rows.push(['role', show(s.lights.role)]);
		rows.push(['response', show(s.lights.response_type)]);
	}
	if (typeof s.classification === 'number') rows.push(['class', String(s.classification)]);
	return rows;
}

/** The decoded J2735 body as indented JSON, capped so a window cannot be flooded. */
export function jsonPretty(decoded: Record<string, unknown> | null | undefined, limit = 6000): string {
	if (!decoded) return '';
	let text: string;
	try {
		text = JSON.stringify(decoded, null, 1);
	} catch {
		return String(decoded);
	}
	return text.length > limit ? text.slice(0, limit) + '\n…' : text;
}
