/**
 * Background-traffic control (the SUMO bridge on RFS) for the editor's
 * "Background traffic" block: status polling plus start and stop.
 *
 *   -> bg_traffic_status | bg_traffic_start | bg_traffic_stop
 *   <- bg_traffic {available, label, state, running, since, uptime_s, last_start, last_stop, last_line, error?}
 */

import { writable } from 'svelte/store';
import { registerDriveMessageHandler, sendDriveMessage } from './driveSocket';
import type { BgTrafficStatus, DriveMessage } from '$lib/types';

export const bgTraffic = writable<BgTrafficStatus | null>(null);
/** 'start' or 'stop' while a request is on its way. */
export const bgTrafficBusy = writable<'start' | 'stop' | null>(null);

registerDriveMessageHandler('bg_traffic', (msg: DriveMessage) => {
	bgTraffic.set(msg as unknown as BgTrafficStatus);
	bgTrafficBusy.set(null);
});

export function requestBgTraffic(): boolean {
	return sendDriveMessage({ type: 'bg_traffic_status' });
}

export function startBgTraffic(): boolean {
	bgTrafficBusy.set('start');
	return sendDriveMessage({ type: 'bg_traffic_start' });
}

export function stopBgTraffic(): boolean {
	bgTrafficBusy.set('stop');
	return sendDriveMessage({ type: 'bg_traffic_stop' });
}

export function resetBgTraffic(): void {
	bgTraffic.set(null);
	bgTrafficBusy.set(null);
}

/** "2 h 41 m" style uptime for the status line. */
export function formatUptime(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
	const s = Math.max(0, Math.round(seconds));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	if (h > 0) return `${h} h ${String(m).padStart(2, '0')} m`;
	if (m > 0) return `${m} m ${String(s % 60).padStart(2, '0')} s`;
	return `${s} s`;
}
