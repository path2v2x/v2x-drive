import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import DrivingHud from '$lib/components/DrivingHud.svelte';
import SideRail from '$lib/components/SideRail.svelte';
import type { RailItem, VehicleTelemetry } from '$lib/types';

function telemetry(): VehicleTelemetry {
	return {
		speed: 44.6,
		gear: -1,
		pos: [100.3, -9.1, 6.1],
		rot: [0, 0, 0],
		steer: 0.5,
		throttle: 0.75,
		brake: 0,
		nearby_actors: [
			{ id: 7, pos: [103.3, -5.1], yaw: 0, type: 'erv', type_id: 'vehicle.ambulance.ford' } as never,
			{ id: 8, pos: [160, -9.1], yaw: 0, type: 'traffic', type_id: 'vehicle.lincoln.mkz' } as never
		],
		hud: {
			server_fps: 19.6,
			frame: 1200,
			sim_time: 65.4,
			speed_limit: 40,
			compass: 90,
			accel: [0.5, -0.25, 9.81],
			gyro: [0.01, 0.02, -0.3],
			imu: true,
			gnss: [37.915201, -122.334812],
			height: 6.1,
			hand_brake: true,
			manual: false,
			collision: { intensity: 123.4, with: 'Prop Trafficcone01', count: 1 },
			vehicles: 12,
			vehicle: 'Tesla Model3',
			map: 'Richmond_Field_Station'
		}
	};
}

describe('DrivingHud', () => {
	it('renders the manual_control.py lines from the telemetry', () => {
		render(DrivingHud, { props: { telemetry: telemetry(), clientFps: 18.4, inputMode: 'keyboard', connected: true } });
		expect(screen.getByTestId('hud-server-fps').textContent).toBe('20 FPS');
		expect(screen.getByTestId('hud-client-fps').textContent).toBe('18 FPS');
		expect(screen.getByTestId('hud-vehicle').textContent).toBe('Tesla Model3');
		expect(screen.getByTestId('hud-map').textContent).toBe('Richmond_Field_Station');
		expect(screen.getByTestId('hud-sim-time').textContent).toBe('00:01:05');
		expect(screen.getByTestId('hud-speed').textContent).toBe('  45 km/h  (limit 40)');
		expect(screen.getByTestId('hud-compass').textContent).toBe('  90° E');
		expect(screen.getByTestId('hud-accel').textContent).toBe('(  0.50, -0.25,  9.81)');
		expect(screen.getByTestId('hud-gyro').textContent).toBe('(  0.01,  0.02, -0.30)');
		expect(screen.getByTestId('hud-location').textContent).toBe('(  100.3,   -9.1)');
		expect(screen.getByTestId('hud-gnss').textContent).toBe('(37.915201, -122.334812)');
		expect(screen.getByTestId('hud-height').textContent).toBe('6 m');
		expect(screen.getByTestId('hud-gear').textContent).toBe('R');
		expect(screen.getByTestId('hud-reverse').getAttribute('aria-checked')).toBe('true');
		expect(screen.getByTestId('hud-collision').textContent).toContain('Prop Trafficcone01 (123)');
		expect(screen.getByTestId('hud-vehicle-count').textContent).toBe('12');
		const rows = screen.getAllByTestId('hud-nearby').map((r) => r.textContent);
		expect(rows).toEqual(['   5 m  Ambulance Ford', '  60 m  Lincoln Mkz']);
	});

	it('copes with a server that sends no hud block', () => {
		const t = telemetry();
		delete t.hud;
		t.nearby_actors = [];
		render(DrivingHud, { props: { telemetry: t } });
		expect(screen.getByTestId('hud-server-fps').textContent).toBe('- FPS');
		expect(screen.getByTestId('hud-sim-time').textContent).toBe('--:--:--');
		expect(screen.getByTestId('hud-compass').textContent).toBe('-');
		expect(screen.getByText('none within 200 m')).toBeInTheDocument();
		expect(screen.queryByTestId('hud-collision')).toBeNull();
	});
});

describe('SideRail', () => {
	it('renders buttons with labels, marks the active one and calls the handlers', async () => {
		const clicks: string[] = [];
		const items: RailItem[] = [
			{ id: 'weather', label: 'Weather', icon: 'M0 0h1', active: true, tone: 'info', onClick: () => clicks.push('weather') },
			{ id: 'chase', label: 'Chase camera', text: 'CHASE', onClick: () => clicks.push('chase') }
		];
		const actions: RailItem[] = [{ id: 'end', label: 'End session', icon: 'M0 0h1', tone: 'danger', onClick: () => clicks.push('end') }];
		render(SideRail, { props: { items, side: 'right', actions, footer: 'ALT 120m' } });
		const weather = screen.getByRole('button', { name: 'Weather' });
		expect(weather.getAttribute('aria-pressed')).toBe('true');
		expect(screen.getByRole('button', { name: 'Chase camera' }).textContent).toContain('CHASE');
		await fireEvent.click(weather);
		await fireEvent.click(screen.getByRole('button', { name: 'End session' }));
		expect(clicks).toEqual(['weather', 'end']);
		expect(screen.getByTestId('rail-footer').textContent).toBe('ALT 120m');
		expect(screen.getByTestId('side-rail-right')).toBeInTheDocument();
	});
});
