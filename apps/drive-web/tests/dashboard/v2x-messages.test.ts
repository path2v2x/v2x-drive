import { describe, expect, it } from 'vitest';
import { HOLD_MS, deriveV2xMessages, relativeBearing, wrapDeg } from '$lib/v2xMessages';
import type { V2xPerspectiveVehicle } from '$lib/types';

function vehicle(over: Partial<V2xPerspectiveVehicle>): V2xPerspectiveVehicle {
	return {
		key: 'erv',
		actor_id: 1,
		name: 'PATH-M-1',
		kind: 'voices',
		subkind: '',
		org: 'PATH',
		emergency: true,
		pos: [-80, 0, 0],
		yaw: 0,
		speed_mps: 20,
		tx: {
			msg_type: 'BSM',
			t_rx: 0,
			age_s: 0.1,
			rate_hz: 10,
			count: 100,
			source: 'tena',
			size: 50,
			summary: null,
			error: null
		},
		rx: [],
		rx_count: 0,
		...over
	};
}

// the ego sits at the origin heading along +x; a sender at negative x is behind it
const ego = { x: 0, y: 0, yaw: 0 };

describe('relativeBearing', () => {
	it('puts a point straight behind at 180 degrees', () => {
		expect(Math.abs(relativeBearing(ego, -50, 0).rel)).toBe(180);
		expect(relativeBearing(ego, 50, 0).rel).toBe(0);
		expect(relativeBearing(ego, 0, 50).rel).toBe(90);
	});
	it('wraps angles', () => {
		expect(wrapDeg(190)).toBe(-170);
		expect(wrapDeg(-190)).toBe(170);
	});
});

describe('deriveV2xMessages', () => {
	it('asks to pull over for an emergency vehicle behind and heading the same way', () => {
		const out = deriveV2xMessages(ego, [vehicle({})], 300, new Map(), new Map(), 1000);
		expect(out).toHaveLength(1);
		expect(out[0].kind).toBe('pull_over');
		expect(out[0].text).toContain('PATH-M-1');
		expect(out[0].text).toContain('Pull over');
		expect(Math.round(out[0].distance_m)).toBe(80);
	});

	it('says nothing for an emergency vehicle ahead', () => {
		const out = deriveV2xMessages(ego, [vehicle({ pos: [80, 0, 0] })], 300, new Map(), new Map(), 1000);
		expect(out).toHaveLength(0);
	});

	it('hears nothing outside the reception range or from a silent vehicle', () => {
		expect(deriveV2xMessages(ego, [vehicle({ pos: [-400, 0, 0] })], 300, new Map(), new Map(), 1000)).toHaveLength(0);
		expect(deriveV2xMessages(ego, [vehicle({ tx: null })], 300, new Map(), new Map(), 1000)).toHaveLength(0);
	});

	it('ignores ordinary cars and the ego itself', () => {
		expect(deriveV2xMessages(ego, [vehicle({ emergency: false })], 300, new Map(), new Map(), 1000)).toHaveLength(0);
		expect(deriveV2xMessages(ego, [vehicle({ subkind: 'ego' })], 300, new Map(), new Map(), 1000)).toHaveLength(0);
	});

	it('counts a closing distance as approaching even when the heading differs', () => {
		const tracks = new Map();
		const active = new Map();
		// heading across our path (90 degrees off) but getting closer snapshot by snapshot
		expect(deriveV2xMessages(ego, [vehicle({ pos: [-90, 0, 0], yaw: 90 })], 300, tracks, active, 1000)).toHaveLength(0);
		expect(deriveV2xMessages(ego, [vehicle({ pos: [-85, 0, 0], yaw: 90 })], 300, tracks, active, 1500)).toHaveLength(1);
	});

	it('holds a message briefly after the condition stops, then drops it', () => {
		const tracks = new Map();
		const active = new Map();
		expect(deriveV2xMessages(ego, [vehicle({})], 300, tracks, active, 1000)).toHaveLength(1);
		// the vehicle passed: now ahead
		expect(deriveV2xMessages(ego, [vehicle({ pos: [20, 0, 0] })], 300, tracks, active, 1000 + HOLD_MS - 1)).toHaveLength(1);
		expect(deriveV2xMessages(ego, [vehicle({ pos: [20, 0, 0] })], 300, tracks, active, 1000 + HOLD_MS + 1)).toHaveLength(0);
	});
});
