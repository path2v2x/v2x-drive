import { describe, expect, it } from 'vitest';
import {
	formatHeading,
	formatPosition,
	formatRxTime,
	formatSpeed,
	isBsmSummary,
	lightsLabel,
	rowLine,
	shortTypeId
} from '$lib/v2xFormat';
import type { V2xBsmSummary, V2xMessageRow } from '$lib/types';

function summary(overrides: Partial<V2xBsmSummary> = {}): V2xBsmSummary {
	return {
		id: '11111110',
		msg_cnt: 0,
		sec_mark: 46000,
		lat: 37.918233,
		lon: -122.333643,
		elev_m: 6.2,
		speed_mps: 3.26,
		heading_deg: 201.8875,
		brakes: false,
		transmission: 'forwardGears',
		size_m: null,
		lights: null,
		coords: 'wgs84',
		...overrides
	};
}

function row(overrides: Partial<V2xMessageRow> = {}): V2xMessageRow {
	return {
		key: 'PATH-M-1',
		seq: 1,
		t_rx: 1760000000.123,
		msg_type: 'BSM',
		source: 'tena',
		sender: 'PATH-M-1',
		uuid: 'u1',
		raw_hex: '001431',
		size: 52,
		summary: summary(),
		error: null,
		tx: null,
		...overrides
	};
}

describe('v2x format helpers', () => {
	it('formats units', () => {
		expect(formatSpeed(3.26)).toBe('11.7 km/h');
		expect(formatSpeed(null)).toBe('–');
		expect(formatHeading(201.8875)).toBe('201.9°');
		expect(formatHeading(undefined)).toBe('–');
		expect(formatRxTime(1760000000.123)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
		expect(formatRxTime(Number.NaN)).toBe('–');
		expect(shortTypeId('vehicle.ambulance.ford')).toBe('ambulance.ford');
		expect(shortTypeId('walker.pedestrian.0001')).toBe('walker.pedestrian.0001');
	});

	it('describes positions by coordinate frame', () => {
		expect(formatPosition(summary())).toBe('37.918233, -122.333643 · 6.2 m');
		expect(formatPosition(summary({ lat: 0.0007912, lon: 0.0004081, coords: 'origin-relative' }))).toBe(
			'origin-relative 0.0007912, 0.0004081'
		);
		expect(formatPosition(summary({ lat: null }))).toBe('position unavailable');
	});

	it('labels emergency lights', () => {
		expect(lightsLabel(summary())).toBeNull();
		const on = { siren: 'inUse', lights: 'inUse', multi: 'unavailable', role: 'ambulance', response_type: null, events: null };
		expect(lightsLabel(summary({ lights: on }))).toBe('SIREN+LIGHTS');
		expect(lightsLabel(summary({ lights: { ...on, siren: 'notInUse' } }))).toBe('LIGHTS');
		expect(lightsLabel(summary({ lights: { ...on, siren: 'unavailable', lights: 'unavailable' } }))).toBe('off');
	});

	it('builds one-line row descriptions', () => {
		expect(rowLine(row())).toBe('id 11111110 · 11.7 km/h · 201.9° · 37.918233,-122.333643');
		const on = { siren: 'inUse', lights: 'inUse', multi: null, role: null, response_type: null, events: null };
		expect(
			rowLine(row({ summary: summary({ lights: on, brakes: true, lat: 0.0007912, lon: 0.0004081, coords: 'origin-relative' }) }))
		).toBe('id 11111110 · 11.7 km/h · 201.9° · rel 0.000791,0.000408 · SIREN+LIGHTS · BRAKE');
		expect(rowLine(row({ msg_type: 'SPaT', summary: { message_id: 19 }, size: 90, source: 'udp' }))).toBe('SPaT · 90 B · udp');
		expect(isBsmSummary({ message_id: 19 })).toBe(false);
		expect(isBsmSummary(summary())).toBe(true);
	});
});
