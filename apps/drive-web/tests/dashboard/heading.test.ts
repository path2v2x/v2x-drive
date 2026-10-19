import { describe, expect, it } from 'vitest';
import { carlaToGps, carlaYawToBearing } from '$lib/geo';

describe('CARLA heading to map bearing', () => {
	it('maps CARLA yaw (0 = +x east, 90 = +y south) onto a bearing clockwise from north', () => {
		expect(carlaYawToBearing(0)).toBe(90); // +x is east
		expect(carlaYawToBearing(90)).toBe(180); // +y is south
		expect(carlaYawToBearing(-90)).toBe(0); // -y is north
		expect(carlaYawToBearing(180)).toBe(270); // -x is west
		expect(carlaYawToBearing(-180)).toBe(270);
		expect(carlaYawToBearing(450)).toBe(180);
		expect(carlaYawToBearing(-270)).toBe(180);
	});

	it('agrees with the position conversion: moving along +y goes south', () => {
		const [, lat0] = carlaToGps(0, 0, 37.9, -122.3);
		const [, lat1] = carlaToGps(0, 100, 37.9, -122.3);
		const [lon0] = carlaToGps(0, 0, 37.9, -122.3);
		const [lon1] = carlaToGps(100, 0, 37.9, -122.3);
		expect(lat1).toBeLessThan(lat0);
		expect(lon1).toBeGreaterThan(lon0);
	});
});
