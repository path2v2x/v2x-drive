import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import V2xVehicleListPanel from '$lib/components/V2xVehicleListPanel.svelte';
import { resetWindowManager, windows } from '$lib/stores/windowManager';
import {
	resetV2xViewer,
	streamWindowId,
	v2xFeedStatus,
	v2xStreams,
	v2xVehicles
} from '$lib/stores/v2xViewer';
import type { V2xFeedStatus, V2xVehicleEntry } from '$lib/types';

function vehicle(key: string, kind: V2xVehicleEntry['kind'], extra: Partial<V2xVehicleEntry> = {}): V2xVehicleEntry {
	return {
		key,
		actor_id: 1,
		role: key,
		type_id: 'vehicle.lincoln.mkz',
		kind,
		subkind: 'scenario',
		org: key.split('-')[0],
		bsm_id: '11111110',
		pos: [0, 0, 0],
		yaw: 0,
		speed_mps: 4.0,
		emergency: false,
		special_lights: null,
		feed: { source: 'tena', last_rx: 1, age_s: 0.1, rate_hz: 10, count: 5 },
		in_world: true,
		...extra
	};
}

function feed(connected: boolean): V2xFeedStatus {
	return {
		decoder: { available: true, error: null },
		json_stream: { connected, records_per_s: connected ? 60 : 0 },
		udp: { datagrams_per_s: 0 },
		roster: { scenario: 'Cohort 2 - ERV', vehicles: 18 },
		hub: { total: 0, senders: 0, subscriptions: 0 },
		keeper: null
	};
}

beforeEach(() => {
	resetV2xViewer();
	resetWindowManager();
});

describe('V2xVehicleListPanel', () => {
	it('explains an empty list in terms of the DT streamer', () => {
		v2xFeedStatus.set(feed(false));
		render(V2xVehicleListPanel);
		expect(screen.getByTestId('v2x-empty')).toHaveTextContent('JSON streamer is not connected');
		expect(screen.getByTestId('v2x-feed-status')).toHaveTextContent('stream off');
	});

	it('groups vehicles and opens or closes a stream window from the checkbox', async () => {
		v2xFeedStatus.set(feed(true));
		v2xVehicles.set([
			vehicle('PATH-BG-5', 'voices', { subkind: 'ego' }),
			vehicle('PATH-M-1', 'voices', { emergency: true, special_lights: true, type_id: 'vehicle.ambulance.ford' }),
			vehicle('PATH-BG-1', 'voices'),
			vehicle('VW-M-1', 'ghost', { feed: { source: 'stale', last_rx: 1, age_s: 12, rate_hz: 0, count: 5 } })
		]);
		render(V2xVehicleListPanel);
		expect(screen.getByText(/Your ego/)).toBeInTheDocument();
		expect(screen.getByText(/VOICES \(ours\)/)).toBeInTheDocument();
		expect(screen.getByText(/Ghosts \(other orgs\)/)).toBeInTheDocument();
		expect(screen.getAllByTestId('v2x-vehicle-row')).toHaveLength(4);
		expect(screen.getByText('LIGHTS')).toBeInTheDocument();
		expect(screen.getByText('stale 12s')).toBeInTheDocument();
		expect(screen.getByTestId('v2x-feed-status')).toHaveTextContent('stream 60/s');
		expect(screen.getByTestId('v2x-feed-status')).toHaveTextContent('Cohort 2 - ERV');

		const box = screen.getByLabelText('Show messages from PATH-M-1');
		await fireEvent.click(box);
		expect(get(windows).some((w) => w.id === streamWindowId('PATH-M-1'))).toBe(true);
		expect(get(v2xStreams).has('PATH-M-1')).toBe(true);
		expect((box as HTMLInputElement).checked).toBe(true);

		await fireEvent.click(box);
		expect(get(windows).some((w) => w.id === streamWindowId('PATH-M-1'))).toBe(false);
		expect(get(v2xStreams).has('PATH-M-1')).toBe(false);

		await fireEvent.click(screen.getAllByRole('button', { name: 'open all' })[0]);
		expect(get(v2xStreams).has('PATH-M-1')).toBe(true);
		expect(get(v2xStreams).has('PATH-BG-1')).toBe(true);
	});
});
