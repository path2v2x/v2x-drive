/**
 * V2X messages for the driver: what the car's onboard unit would tell the driver after
 * reading the BSMs it hears, as opposed to the BSM fields themselves (the V2X panel).
 *
 * The ego hears every transmitting vehicle inside the reception radius (the same radius the
 * V2X panel uses).  For now one message exists: an emergency vehicle that is behind the ego
 * and coming its way asks the driver to pull over.  "Coming its way" is either a heading
 * within ALIGNED_DEG of the ego's or a distance that shrank since the last snapshot.  A
 * message stays up HOLD_MS after its condition last held, so it does not flicker at the edge
 * of the cone or the range.
 */
import type { V2xPerspectiveVehicle } from '$lib/types';

export interface EgoPose {
	x: number;
	y: number;
	/** CARLA yaw, degrees */
	yaw: number;
}

export interface V2xMessage {
	id: string;
	kind: 'pull_over';
	title: string;
	text: string;
	sender: string;
	distance_m: number;
	rel_bearing_deg: number;
	siren: boolean;
	/** when the message first appeared (ms) */
	since: number;
}

export interface SenderTrack {
	dist: number;
	t: number;
}

interface ActiveMessage extends V2xMessage {
	lastTrue: number;
}

/** |relative bearing| at or above this is "behind" the ego. */
export const BEHIND_DEG = 120;
/** a sender heading within this of the ego's is coming the ego's way */
export const ALIGNED_DEG = 60;
/** a message stays this long after its condition last held (ms) */
export const HOLD_MS = 3000;
/** the distance has to shrink by this much between snapshots to count as closing (m) */
export const CLOSING_M = 0.3;

export function wrapDeg(a: number): number {
	return ((((a + 180) % 360) + 360) % 360) - 180;
}

/** Distance and the bearing of a point relative to the ego's heading (CARLA frame, + = right). */
export function relativeBearing(ego: EgoPose, x: number, y: number): { dist: number; rel: number } {
	const dx = x - ego.x,
		dy = y - ego.y;
	const dist = Math.hypot(dx, dy);
	const bearing = (Math.atan2(dy, dx) * 180) / Math.PI;
	return { dist, rel: wrapDeg(bearing - ego.yaw) };
}

export function pullOverText(sender: string, distance_m: number, siren: boolean): string {
	const d = Math.round(distance_m);
	return `Emergency vehicle ${sender} approaching from behind${siren ? ' with its siren on' : ''}, ${d} m. Pull over to the right and stop.`;
}

/**
 * The messages the ego should show right now.  ``tracks`` and ``active`` are the caller's
 * state across snapshots (last distance per sender, messages with their last-true time).
 */
export function deriveV2xMessages(
	ego: EgoPose,
	vehicles: V2xPerspectiveVehicle[],
	radiusM: number,
	tracks: Map<string, SenderTrack>,
	active: Map<string, ActiveMessage>,
	now: number
): V2xMessage[] {
	const seen = new Set<string>();
	for (const v of vehicles) {
		if (!v.pos || v.pos.length < 2 || v.subkind === 'ego') continue;
		if (!v.tx) continue; // silent: nothing heard, nothing to say
		const { dist, rel } = relativeBearing(ego, v.pos[0], v.pos[1]);
		if (dist > radiusM) continue; // out of the reception range
		seen.add(v.key);
		const prev = tracks.get(v.key);
		tracks.set(v.key, { dist, t: now });
		if (!v.emergency) continue;
		const behind = Math.abs(rel) >= BEHIND_DEG;
		const aligned = typeof v.yaw === 'number' && Math.abs(wrapDeg(v.yaw - ego.yaw)) <= ALIGNED_DEG;
		const closing = prev !== undefined && prev.dist - dist >= CLOSING_M;
		if (!behind || !(aligned || closing)) continue;
		const id = `pull_over:${v.key}`;
		const siren = Boolean(v.tx.summary?.lights?.siren);
		const existing = active.get(id);
		active.set(id, {
			id,
			kind: 'pull_over',
			title: 'PULL OVER',
			text: pullOverText(v.name, dist, siren),
			sender: v.name,
			distance_m: dist,
			rel_bearing_deg: rel,
			siren,
			since: existing?.since ?? now,
			lastTrue: now
		});
	}
	for (const [key, track] of tracks) {
		if (!seen.has(key) && now - track.t > HOLD_MS) tracks.delete(key);
	}
	const out: V2xMessage[] = [];
	for (const [id, m] of active) {
		if (now - m.lastTrue > HOLD_MS) {
			active.delete(id);
			continue;
		}
		out.push(m);
	}
	out.sort((a, b) => a.distance_m - b.distance_m);
	return out;
}
