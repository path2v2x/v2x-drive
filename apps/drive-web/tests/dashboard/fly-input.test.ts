import { describe, expect, it } from 'vitest';
import { SPEED_MAX, SPEED_MIN, axesFromKeys, buildCameraMove, isFlyKey, mouseTurn, stepSpeed } from '$lib/flyInput';

describe('fly input', () => {
	it('maps keys onto the three axes', () => {
		expect(axesFromKeys(new Set(['w']))).toEqual({ f: 1, r: 0, u: 0 });
		expect(axesFromKeys(new Set(['w', 's']))).toEqual({ f: 0, r: 0, u: 0 });
		expect(axesFromKeys(new Set(['arrowdown', 'd', 'q']))).toEqual({ f: -1, r: 1, u: -1 });
		expect(axesFromKeys(new Set(['a', 'e']))).toEqual({ f: 0, r: -1, u: 1 });
		expect(axesFromKeys(new Set([' ']))).toEqual({ f: 0, r: 0, u: 1 }); // Space: straight up
		expect(isFlyKey(' ')).toBe(true);
		expect(isFlyKey('Shift') && isFlyKey('ArrowLeft') && !isFlyKey('x')).toBe(true);
	});

	it('turns with the mouse and steps the speed within limits', () => {
		expect(mouseTurn(100, -50)).toEqual({ dyaw: 12, dpitch: 6 });
		expect(stepSpeed(20, 1)).toBe(25);
		expect(stepSpeed(20, -1)).toBe(16);
		expect(stepSpeed(SPEED_MAX, 1)).toBe(SPEED_MAX);
		expect(stepSpeed(SPEED_MIN, -1)).toBe(SPEED_MIN);
	});

	it('builds one message per tick and nothing when idle', () => {
		expect(buildCameraMove({ keys: new Set(), dyaw: 0, dpitch: 0, dt: 0.05 })).toBeNull();
		expect(buildCameraMove({ keys: new Set(['w', 'shift']), dyaw: 0.004, dpitch: -3.456, dt: 0.05, boost: true })).toEqual({
			type: 'camera_move',
			dt: 0.05,
			f: 1,
			dpitch: -3.46,
			boost: true
		});
		expect(buildCameraMove({ keys: new Set(), dyaw: 0, dpitch: 0, dt: 0.05, speed: 25 })).toEqual({
			type: 'camera_move',
			dt: 0.05,
			speed: 25
		});
	});
});
