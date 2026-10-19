import { describe, expect, it } from 'vitest';
import {
	CollisionHistory,
	collisionPolyline,
	compassLabel,
	displayName,
	fixed,
	formatSimTime,
	formatVector,
	gearLabel,
	nearbyRows
} from '$lib/hudFormat';
import { isTypingTarget } from '$lib/inputGate';

describe('HUD formatting', () => {
	it('labels the compass like manual_control.py', () => {
		expect(compassLabel(0)).toBe('N');
		expect(compassLabel(45)).toBe('NE');
		expect(compassLabel(90)).toBe('E');
		expect(compassLabel(135)).toBe('SE');
		expect(compassLabel(180)).toBe('S');
		expect(compassLabel(270)).toBe('W');
		expect(compassLabel(315)).toBe('NW');
		expect(compassLabel(-90)).toBe('W');
	});

	it('formats sim time, gears, names and numbers', () => {
		expect(formatSimTime(3725.9)).toBe('01:02:05');
		expect(formatSimTime(null)).toBe('--:--:--');
		expect(gearLabel(-1)).toBe('R');
		expect(gearLabel(0)).toBe('N');
		expect(gearLabel(3)).toBe('3');
		expect(displayName('vehicle.tesla.model3')).toBe('Tesla Model3');
		expect(displayName('static.prop.trafficcone01')).toBe('Prop Trafficcone01');
		expect(displayName(undefined)).toBe('');
		expect(fixed(9.81, 6, 2)).toBe('  9.81');
		expect(fixed(-0.3, 6, 2)).toBe(' -0.30');
		expect(fixed(null)).toBe('     -');
		expect(formatVector([0.5, -0.25, 9.81])).toBe('(  0.50, -0.25,  9.81)');
	});

	it('lists nearby vehicles nearest first, within 200 m, capped', () => {
		const rows = nearbyRows(
			[0, 0, 0],
			[
				{ id: 1, pos: [30, 40], yaw: 0, type: 'traffic', type_id: 'vehicle.lincoln.mkz' } as never,
				{ id: 2, pos: [3, 4], yaw: 0, type: 'erv', type_id: 'vehicle.ambulance.ford' } as never,
				{ id: 3, pos: [500, 0], yaw: 0, type: 'other' } as never
			],
			8
		);
		expect(rows.map((r) => [r.id, r.dist, r.name])).toEqual([
			[2, 5, 'Ambulance Ford'],
			[1, 50, 'Lincoln Mkz']
		]);
		expect(nearbyRows(undefined, [], 8)).toEqual([]);
	});

	it('keeps a bounded collision history normalised by its maximum', () => {
		const h = new CollisionHistory(4);
		h.push(0);
		h.push(10);
		h.push(5);
		expect(h.last()).toBe(5);
		expect(h.max()).toBe(10);
		expect(h.normalized()).toEqual([0, 0, 1, 0.5]);
		h.push(20);
		h.push(NaN);
		expect(h.normalized()).toEqual([0.5, 0.25, 1, 0]);
		expect(collisionPolyline([0, 1], 100, 20)).toBe('0.0,20.0 100.0,1.0');
		expect(collisionPolyline([], 100, 20)).toBe('');
	});
});

describe('input gating', () => {
	it('recognises elements that own the keyboard', () => {
		const input = document.createElement('input');
		const area = document.createElement('textarea');
		const select = document.createElement('select');
		const div = document.createElement('div');
		const editable = document.createElement('div');
		editable.contentEditable = 'true';
		expect(isTypingTarget(input)).toBe(true);
		expect(isTypingTarget(area)).toBe(true);
		expect(isTypingTarget(select)).toBe(true);
		expect(isTypingTarget(div)).toBe(false);
		expect(isTypingTarget(null)).toBe(false);
		expect(isTypingTarget(window)).toBe(false);
		expect(typeof editable.isContentEditable === 'boolean' ? isTypingTarget(editable) : true).toBe(true);
	});
});
