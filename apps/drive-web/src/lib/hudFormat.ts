/**
 * Formatting for the driving HUD (the pygame manual_control.py look).
 * Pure functions, so the HUD component stays a thin renderer.
 */

import type { NearbyActor } from '$lib/types';

/** manual_control.py's heading letters: N/S from the first half, E/W from the second. */
export function compassLabel(deg: number): string {
	const c = ((deg % 360) + 360) % 360;
	let label = '';
	if (c > 270.5 || c < 89.5) label += 'N';
	if (c > 90.5 && c < 269.5) label += 'S';
	if (c > 0.5 && c < 179.5) label += 'E';
	if (c > 180.5 && c < 359.5) label += 'W';
	return label;
}

/** Seconds of simulation time as hh:mm:ss (manual_control.py shows a timedelta). */
export function formatSimTime(seconds: number | null | undefined): string {
	if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '--:--:--';
	const total = Math.floor(seconds);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = total % 60;
	return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function gearLabel(gear: number): string {
	if (gear < 0) return 'R';
	if (gear === 0) return 'N';
	return String(gear);
}

/** `vehicle.tesla.model3` -> `Tesla Model3` (the server does the same for its own lines). */
export function displayName(typeId: string | undefined | null): string {
	return String(typeId ?? '')
		.replace(/_/g, '.')
		.split('.')
		.slice(1)
		.map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
		.join(' ');
}

/** Fixed-width number like pygame's `% 5.1f`: sign, padding, one decimal. */
export function fixed(v: number | null | undefined, width = 6, decimals = 1): string {
	if (typeof v !== 'number' || !Number.isFinite(v)) return '-'.padStart(width, ' ');
	return v.toFixed(decimals).padStart(width, ' ');
}

export function formatVector(v: [number, number, number] | undefined | null, decimals = 2): string {
	if (!v) return '(   -   ,   -   ,   -   )';
	return `(${fixed(v[0], 6, decimals)},${fixed(v[1], 6, decimals)},${fixed(v[2], 6, decimals)})`;
}

/** Nearby vehicles sorted by distance, nearest first, as manual_control.py lists them. */
export function nearbyRows(
	pos: [number, number, number] | undefined,
	actors: NearbyActor[] | undefined,
	max = 8
): { id: number; dist: number; name: string; type: string }[] {
	if (!pos || !actors?.length) return [];
	return actors
		.map((a) => ({
			id: a.id,
			dist: Math.hypot(a.pos[0] - pos[0], a.pos[1] - pos[1]),
			name: displayName((a as NearbyActor & { type_id?: string }).type_id) || a.type,
			type: a.type
		}))
		.filter((r) => r.dist <= 200)
		.sort((a, b) => a.dist - b.dist)
		.slice(0, max);
}

/** The last ``size`` collision intensities, newest last; what the HUD's graph draws. */
export class CollisionHistory {
	private readonly values: number[];

	constructor(public readonly size = 200) {
		this.values = new Array(size).fill(0);
	}

	/** Record one frame: the intensity of the collision in it, 0 when there was none. */
	push(intensity: number): void {
		this.values.push(Math.max(0, Number.isFinite(intensity) ? intensity : 0));
		if (this.values.length > this.size) this.values.shift();
	}

	/** Values scaled to 0..1 by the window's maximum (manual_control.py normalises the same way). */
	normalized(): number[] {
		const max = Math.max(1, ...this.values);
		return this.values.map((v) => v / max);
	}

	last(): number {
		return this.values[this.values.length - 1] ?? 0;
	}

	max(): number {
		return Math.max(0, ...this.values);
	}
}

/** SVG polyline points for the collision graph, newest sample at the right edge. */
export function collisionPolyline(normalized: number[], width: number, height: number): string {
	if (normalized.length === 0) return '';
	const step = normalized.length > 1 ? width / (normalized.length - 1) : 0;
	return normalized
		.map((v, i) => `${(i * step).toFixed(1)},${(height - v * (height - 1)).toFixed(1)}`)
		.join(' ');
}
