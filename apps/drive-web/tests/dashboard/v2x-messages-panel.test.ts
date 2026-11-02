import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it } from 'vitest';
import V2xMessagesPanel from '$lib/components/V2xMessagesPanel.svelte';
import { telemetry } from '$lib/stores/driveSocket';
import { v2xPerspective, v2xRadiusM } from '$lib/stores/v2xPerspective';
import type { V2xPerspectiveMessage, V2xPerspectiveVehicle } from '$lib/types';

function erv(pos: number[]): V2xPerspectiveVehicle {
	return {
		key: 'erv',
		actor_id: 1,
		name: 'PATH-M-1',
		kind: 'voices',
		subkind: '',
		org: 'PATH',
		emergency: true,
		pos,
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
		rx_count: 0
	};
}

function snapshot(vehicles: V2xPerspectiveVehicle[]): V2xPerspectiveMessage {
	return { type: 'v2x_perspective', active: true, radius_m: 300, detail: 'simple', vehicles } as unknown as V2xPerspectiveMessage;
}

beforeEach(() => {
	// the ego sits at the origin heading along +x
	telemetry.update((t) => ({ ...t, pos: [0, 0, 0], rot: [0, 0, 0] }));
	v2xRadiusM.set(300);
	v2xPerspective.set(null);
});

describe('V2xMessagesPanel', () => {
	it('shows the pull-over message for an emergency vehicle coming up behind', () => {
		v2xPerspective.set(snapshot([erv([-80, 0, 0])]));
		render(V2xMessagesPanel);
		const panel = screen.getByTestId('v2x-messages');
		expect(panel).toHaveTextContent('V2X messages · 1');
		expect(panel).toHaveTextContent('PULL OVER');
		expect(panel).toHaveTextContent('PATH-M-1');
		expect(panel).toHaveTextContent('Pull over to the right and stop');
		expect(panel).toHaveTextContent('within 300 m');
	});

	it('shows no messages when nothing relevant is heard', () => {
		v2xPerspective.set(snapshot([erv([80, 0, 0])]));
		render(V2xMessagesPanel);
		const panel = screen.getByTestId('v2x-messages');
		expect(panel).toHaveTextContent('V2X messages · 0');
		expect(panel).toHaveTextContent('no messages');
		expect(screen.queryByTestId('v2x-message')).toBeNull();
	});
});
