/**
 * Display helpers for the V2X message viewer (pure functions, unit tested).
 */

import type { V2xBsmSummary, V2xMessageRow } from '$lib/types';

export function isBsmSummary(value: unknown): value is V2xBsmSummary {
	return typeof value === 'object' && value !== null && 'coords' in value;
}

/** Receive time (unix seconds) as a local HH:MM:SS.mmm clock reading. */
export function formatRxTime(tRx: number): string {
	const d = new Date(tRx * 1000);
	if (!Number.isFinite(d.getTime())) return '–';
	return d.toLocaleTimeString([], { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

export function formatSpeed(mps: number | null | undefined, decimals = 1): string {
	if (typeof mps !== 'number' || !Number.isFinite(mps)) return '–';
	return `${(mps * 3.6).toFixed(decimals)} km/h`;
}

export function formatHeading(deg: number | null | undefined, decimals = 1): string {
	if (typeof deg !== 'number' || !Number.isFinite(deg)) return '–';
	return `${deg.toFixed(decimals)}°`;
}

/**
 * Position line. The PATH adapter encodes lat/long relative to the scenario
 * origin (tiny degree values near 0,0); other organizations send WGS-84.
 */
export function formatPosition(s: V2xBsmSummary): string {
	if (typeof s.lat !== 'number' || typeof s.lon !== 'number') return 'position unavailable';
	if (s.coords === 'origin-relative') {
		return `origin-relative ${s.lat.toFixed(7)}, ${s.lon.toFixed(7)}`;
	}
	const elev = typeof s.elev_m === 'number' ? ` · ${s.elev_m.toFixed(1)} m` : '';
	return `${s.lat.toFixed(6)}, ${s.lon.toFixed(6)}${elev}`;
}

/** 'SIREN+LIGHTS' | 'SIREN' | 'LIGHTS' when in use, 'off' when reported off, null when not reported. */
export function lightsLabel(s: V2xBsmSummary): string | null {
	const l = s.lights;
	if (!l) return null;
	const parts: string[] = [];
	if (l.siren === 'inUse') parts.push('SIREN');
	if (l.lights === 'inUse') parts.push('LIGHTS');
	if (parts.length > 0) return parts.join('+');
	return l.siren || l.lights ? 'off' : null;
}

/** One-line description of a message row for the stream list. */
export function rowLine(row: V2xMessageRow): string {
	if (row.msg_type === 'BSM' && isBsmSummary(row.summary)) {
		const s = row.summary;
		const parts = [`id ${s.id ?? '?'}`, formatSpeed(s.speed_mps), formatHeading(s.heading_deg)];
		if (typeof s.lat === 'number' && typeof s.lon === 'number') {
			parts.push(
				s.coords === 'origin-relative'
					? `rel ${s.lat.toFixed(6)},${s.lon.toFixed(6)}`
					: `${s.lat.toFixed(6)},${s.lon.toFixed(6)}`
			);
		}
		const lights = lightsLabel(s);
		if (lights && lights !== 'off') parts.push(lights);
		if (s.brakes) parts.push('BRAKE');
		return parts.join(' · ');
	}
	return `${row.msg_type} · ${row.size} B${row.source === 'udp' ? ' · udp' : ''}`;
}

/** Short blueprint label: vehicle.ambulance.ford -> ambulance.ford. */
export function shortTypeId(typeId: string): string {
	return typeId.startsWith('vehicle.') ? typeId.slice('vehicle.'.length) : typeId;
}
