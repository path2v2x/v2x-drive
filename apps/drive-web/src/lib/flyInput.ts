/**
 * Fly-camera input for the Editor: keys and mouse deltas become one
 * ``camera_move`` message per tick (≤ 20 Hz). Pure functions, tested on their own.
 *
 * W/S (or ↑/↓) move along the view, A/D (←/→) sideways, Space or E straight up
 * and Q straight down the world axis, Shift boosts, the mouse (right button held) turns the camera and
 * the wheel changes the speed in steps.
 */

import type { DriveMessage } from '$lib/types';

export const MOUSE_DEG_PER_PX = 0.12;
export const SPEED_MIN = 1;
export const SPEED_MAX = 200;
export const SPEED_STEP = 1.25;
export const TICK_MS = 50;

const FLY_KEYS = new Set(['w', 'a', 's', 'd', 'q', 'e', ' ', 'shift', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright']);

export function isFlyKey(key: string): boolean {
	return FLY_KEYS.has(key.toLowerCase());
}

export function axesFromKeys(keys: ReadonlySet<string>): { f: number; r: number; u: number } {
	const has = (k: string) => keys.has(k);
	return {
		f: (has('w') || has('arrowup') ? 1 : 0) - (has('s') || has('arrowdown') ? 1 : 0),
		r: (has('d') || has('arrowright') ? 1 : 0) - (has('a') || has('arrowleft') ? 1 : 0),
		u: (has('e') || has(' ') ? 1 : 0) - (has('q') ? 1 : 0)
	};
}

/** Mouse movement in pixels to a turn in degrees: right turns clockwise, up looks up. */
export function mouseTurn(dxPx: number, dyPx: number): { dyaw: number; dpitch: number } {
	return { dyaw: dxPx * MOUSE_DEG_PER_PX, dpitch: -dyPx * MOUSE_DEG_PER_PX };
}

/** The next speed up or down the ladder, within the server's limits. */
export function stepSpeed(speed: number, direction: 1 | -1): number {
	const next = direction > 0 ? speed * SPEED_STEP : speed / SPEED_STEP;
	return Math.round(Math.max(SPEED_MIN, Math.min(SPEED_MAX, next)) * 10) / 10;
}

export interface FlyFrame {
	keys: ReadonlySet<string>;
	dyaw: number;
	dpitch: number;
	dt: number;
	/** only when the user changed it since the last message */
	speed?: number;
	boost?: boolean;
}

/** The camera_move message for one tick, or null when there is nothing to tell the server. */
export function buildCameraMove(frame: FlyFrame): DriveMessage | null {
	const { f, r, u } = axesFromKeys(frame.keys);
	const dyaw = Math.round(frame.dyaw * 100) / 100;
	const dpitch = Math.round(frame.dpitch * 100) / 100;
	if (f === 0 && r === 0 && u === 0 && dyaw === 0 && dpitch === 0 && frame.speed === undefined) return null;
	const msg: DriveMessage = { type: 'camera_move', dt: Math.round(frame.dt * 1000) / 1000 };
	if (f) msg.f = f;
	if (r) msg.r = r;
	if (u) msg.u = u;
	if (dyaw) msg.dyaw = dyaw;
	if (dpitch) msg.dpitch = dpitch;
	if (frame.speed !== undefined) msg.speed = frame.speed;
	if (frame.boost) msg.boost = true;
	return msg;
}
