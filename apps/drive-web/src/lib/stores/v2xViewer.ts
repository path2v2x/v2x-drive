/**
 * V2X message viewer store — the vehicle list and one message stream per
 * opened vehicle window.
 *
 * Protocol (drive WebSocket; server side in
 * apps/drive-server/digital_twin_bridge/v2x_viewer.py):
 *   -> v2x_list_vehicles                <- v2x_vehicle_list   (also pushed on change while watched)
 *   -> v2x_subscribe {keys, detail}     <- v2x_subscribed
 *   -> v2x_unsubscribe {keys}           <- v2x_subscribed
 *   -> v2x_decode {raw_hex}             <- v2x_decoded
 *                                       <- v2x_messages {dropped, batch[]}   (pushed, <= 10/s)
 *
 * Incoming rows are buffered and flushed to the stores at most every FLUSH_MS
 * so a dozen 10 Hz streams do not re-render the windows on every packet.
 */

import { derived, get, writable } from 'svelte/store';
import {
	registerDisconnectHook,
	registerDriveMessageHandler,
	sendDriveMessage
} from './driveSocket';
import {
	closeAllWindows,
	closeWindow,
	isWindowOpen,
	openWindow,
	topWindowId,
	windows
} from './windowManager';
import type {
	DriveMessage,
	V2xDecodedMessage,
	V2xFeedStatus,
	V2xMessageRow,
	V2xMessagesMessage,
	V2xSubscribedMessage,
	V2xVehicleEntry,
	V2xVehicleListMessage
} from '$lib/types';

export const RING_MAX = 200;
export const FLUSH_MS = 100;
export const LIST_LEASE_RENEW_MS = 30_000;
export const LIST_WINDOW_ID = 'v2x:list';
const STREAM_PREFIX = 'v2x:stream:';
const DECODE_CACHE_MAX = 256;

export function streamWindowId(key: string): string {
	return STREAM_PREFIX + key;
}

export function streamKeyOf(windowId: string): string | null {
	return windowId.startsWith(STREAM_PREFIX) ? windowId.slice(STREAM_PREFIX.length) : null;
}

export interface V2xStream {
	key: string;
	rows: V2xMessageRow[];
	total: number;
	paused: boolean;
	last: V2xMessageRow | null;
	rateHz: number;
}

// ── Stores ──

export const v2xVehicles = writable<V2xVehicleEntry[]>([]);
export const v2xFeedStatus = writable<V2xFeedStatus | null>(null);
export const v2xListVersion = writable<number>(-1);
export const v2xListReceivedAt = writable<number>(0);
export const v2xStreams = writable<Map<string, V2xStream>>(new Map());
export const v2xSubscribedKeys = writable<string[]>([]);
export const v2xDetail = writable<'compact' | 'full'>('compact');
export const v2xDecoded = writable<Map<string, V2xDecodedMessage>>(new Map());
/** Messages the server dropped for this connection (all streams) because the client fell behind. */
export const v2xServerDropped = writable<number>(0);
export const v2xViewerOpen = derived(windows, (list) => list.some((w) => w.id === LIST_WINDOW_ID));

// ── Buffering ──

let pending = new Map<string, V2xMessageRow[]>();
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let leaseTimer: ReturnType<typeof setInterval> | null = null;

function emptyStream(key: string): V2xStream {
	return { key, rows: [], total: 0, paused: false, last: null, rateHz: 0 };
}

function rateOf(rows: V2xMessageRow[]): number {
	if (rows.length === 0) return 0;
	const newest = rows[rows.length - 1].t_rx;
	let n = 0;
	for (let i = rows.length - 1; i >= 0; i--) {
		if (rows[i].t_rx < newest - 1.0) break;
		n += 1;
	}
	return n;
}

function scheduleFlush(): void {
	if (flushTimer === null) flushTimer = setTimeout(flushV2xPending, FLUSH_MS);
}

/** Move buffered rows into the streams store (runs on a timer; exported for tests). */
export function flushV2xPending(): void {
	if (flushTimer !== null) {
		clearTimeout(flushTimer);
		flushTimer = null;
	}
	if (pending.size === 0) return;
	const batches = pending;
	pending = new Map();
	v2xStreams.update((map) => {
		const next = new Map(map);
		for (const [key, rows] of batches) {
			const prev = next.get(key);
			if (!prev) continue; // window closed before the flush
			if (prev.paused) {
				next.set(key, { ...prev, total: prev.total + rows.length });
				continue;
			}
			const merged = prev.rows.length + rows.length > RING_MAX
				? [...prev.rows, ...rows].slice(-RING_MAX)
				: [...prev.rows, ...rows];
			next.set(key, {
				...prev,
				rows: merged,
				total: prev.total + rows.length,
				last: rows[rows.length - 1],
				rateHz: rateOf(merged)
			});
		}
		return next;
	});
}

// ── Server messages ──

function onVehicleList(msg: DriveMessage): void {
	const m = msg as V2xVehicleListMessage;
	v2xVehicles.set(Array.isArray(m.vehicles) ? m.vehicles : []);
	v2xFeedStatus.set(m.feed && typeof m.feed === 'object' ? m.feed : null);
	v2xListVersion.set(typeof m.version === 'number' ? m.version : -1);
	v2xListReceivedAt.set(Date.now());
}

function onMessages(msg: DriveMessage): void {
	const m = msg as V2xMessagesMessage;
	if (typeof m.dropped === 'number' && m.dropped > 0) {
		v2xServerDropped.update((n) => n + m.dropped);
	}
	if (!Array.isArray(m.batch)) return;
	const streams = get(v2xStreams);
	for (const row of m.batch) {
		if (!row || typeof row.key !== 'string' || !streams.has(row.key)) continue;
		const list = pending.get(row.key);
		if (list) list.push(row);
		else pending.set(row.key, [row]);
	}
	if (pending.size > 0) scheduleFlush();
}

function onSubscribed(msg: DriveMessage): void {
	const m = msg as V2xSubscribedMessage;
	v2xSubscribedKeys.set(Array.isArray(m.keys) ? m.keys.map(String) : []);
	if (m.detail === 'full' || m.detail === 'compact') v2xDetail.set(m.detail);
}

function onDecoded(msg: DriveMessage): void {
	const m = msg as V2xDecodedMessage;
	if (typeof m.raw_hex !== 'string') return;
	v2xDecoded.update((map) => {
		const next = new Map(map);
		next.set(m.raw_hex, m);
		while (next.size > DECODE_CACHE_MAX) {
			const oldest = next.keys().next().value;
			if (oldest === undefined) break;
			next.delete(oldest);
		}
		return next;
	});
}

registerDriveMessageHandler('v2x_vehicle_list', onVehicleList);
registerDriveMessageHandler('v2x_messages', onMessages);
registerDriveMessageHandler('v2x_subscribed', onSubscribed);
registerDriveMessageHandler('v2x_decoded', onDecoded);
registerDisconnectHook(resetV2xViewer);

// ── Lease: the server pushes list changes only while the client keeps asking ──

function startLease(): void {
	if (leaseTimer !== null) return;
	leaseTimer = setInterval(() => {
		if (isWindowOpen(LIST_WINDOW_ID)) requestV2xVehicleList();
		else stopLease();
	}, LIST_LEASE_RENEW_MS);
}

function stopLease(): void {
	if (leaseTimer !== null) {
		clearInterval(leaseTimer);
		leaseTimer = null;
	}
}

// ── Actions ──

export function requestV2xVehicleList(): boolean {
	return sendDriveMessage({ type: 'v2x_list_vehicles' });
}

export function openV2xViewer(): void {
	openWindow(LIST_WINDOW_ID, { x: 16, y: 72, w: 400, h: 460 });
	requestV2xVehicleList();
	startLease();
}

export function closeV2xViewer(): void {
	closeWindow(LIST_WINDOW_ID);
	stopLease();
}

export function toggleV2xViewer(): void {
	if (isWindowOpen(LIST_WINDOW_ID)) closeV2xViewer();
	else openV2xViewer();
}

export function openV2xStream(key: string): void {
	if (!key) return;
	v2xStreams.update((map) => {
		if (map.has(key)) return map;
		return new Map(map).set(key, emptyStream(key));
	});
	openWindow(streamWindowId(key), { w: 560, h: 380 });
	sendDriveMessage({ type: 'v2x_subscribe', keys: [key], detail: get(v2xDetail) });
}

export function openV2xStreams(keys: string[]): void {
	for (const key of keys) openV2xStream(key);
}

export function closeV2xStream(key: string): void {
	closeWindow(streamWindowId(key));
	pending.delete(key);
	v2xStreams.update((map) => {
		if (!map.has(key)) return map;
		const next = new Map(map);
		next.delete(key);
		return next;
	});
	sendDriveMessage({ type: 'v2x_unsubscribe', keys: [key] });
}

export function toggleV2xStream(key: string): void {
	if (isWindowOpen(streamWindowId(key))) closeV2xStream(key);
	else openV2xStream(key);
}

/** Compact rows carry the summary only; full rows also carry the decoded J2735 body. */
export function setV2xDetail(detail: 'compact' | 'full'): void {
	v2xDetail.set(detail);
	const keys = [...get(v2xStreams).keys()];
	if (keys.length > 0) sendDriveMessage({ type: 'v2x_subscribe', keys, detail });
}

export function pauseV2xStream(key: string, paused?: boolean): void {
	v2xStreams.update((map) => {
		const prev = map.get(key);
		if (!prev) return map;
		return new Map(map).set(key, { ...prev, paused: paused ?? !prev.paused });
	});
}

export function clearV2xStream(key: string): void {
	pending.delete(key);
	v2xStreams.update((map) => {
		const prev = map.get(key);
		if (!prev) return map;
		return new Map(map).set(key, { ...prev, rows: [], total: 0, last: null, rateHz: 0 });
	});
}

/** The buffered rows of one stream as JSON Lines. */
export function exportV2xStream(key: string): string {
	const stream = get(v2xStreams).get(key);
	if (!stream || stream.rows.length === 0) return '';
	return stream.rows.map((row) => JSON.stringify(row)).join('\n') + '\n';
}

/** Ask the server to decode one frame (rows in compact mode); cached per frame. */
export function requestV2xDecode(rawHex: string): boolean {
	if (!rawHex || get(v2xDecoded).has(rawHex)) return false;
	return sendDriveMessage({ type: 'v2x_decode', raw_hex: rawHex });
}

/** Close the topmost V2X window (Escape); false when no V2X window is open. */
export function closeTopV2xWindow(): boolean {
	const id = topWindowId();
	if (!id || !id.startsWith('v2x:')) return false;
	if (id === LIST_WINDOW_ID) {
		closeV2xViewer();
		return true;
	}
	const key = streamKeyOf(id);
	if (key !== null) closeV2xStream(key);
	else closeWindow(id);
	return true;
}

/** Drop every stream, window and cached list (socket closed or test reset). */
export function resetV2xViewer(): void {
	stopLease();
	if (flushTimer !== null) {
		clearTimeout(flushTimer);
		flushTimer = null;
	}
	pending = new Map();
	v2xStreams.set(new Map());
	v2xSubscribedKeys.set([]);
	v2xVehicles.set([]);
	v2xFeedStatus.set(null);
	v2xListVersion.set(-1);
	v2xListReceivedAt.set(0);
	v2xDecoded.set(new Map());
	v2xServerDropped.set(0);
	closeAllWindows('v2x:');
}

// ── Grouping for the list panel ──

export type V2xGroupId = 'ego' | 'voices' | 'ghost' | 'drive' | 'other';

export interface V2xGroup {
	id: V2xGroupId;
	label: string;
	vehicles: V2xVehicleEntry[];
}

const GROUP_LABELS: Record<V2xGroupId, string> = {
	ego: 'Your ego',
	voices: 'VOICES (ours)',
	ghost: 'Ghosts (other orgs)',
	drive: 'Drive server',
	other: 'Other'
};

export function groupV2xVehicles(list: V2xVehicleEntry[]): V2xGroup[] {
	const buckets: Record<V2xGroupId, V2xVehicleEntry[]> = { ego: [], voices: [], ghost: [], drive: [], other: [] };
	for (const v of list) {
		if (v.subkind === 'ego') buckets.ego.push(v);
		else if (v.kind === 'voices') buckets.voices.push(v);
		else if (v.kind === 'ghost') buckets.ghost.push(v);
		else if (v.kind === 'drive') buckets.drive.push(v);
		else buckets.other.push(v);
	}
	return (Object.keys(buckets) as V2xGroupId[])
		.filter((id) => buckets[id].length > 0)
		.map((id) => ({ id, label: GROUP_LABELS[id], vehicles: buckets[id] }));
}
