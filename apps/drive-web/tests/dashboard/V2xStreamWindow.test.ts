import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import V2xStreamWindow from '$lib/components/V2xStreamWindow.svelte';
import { openWindow, resetWindowManager } from '$lib/stores/windowManager';
import { resetV2xViewer, streamWindowId, v2xStreams, v2xVehicles } from '$lib/stores/v2xViewer';
import type { V2xMessageRow, V2xVehicleEntry } from '$lib/types';

const KEY = 'PATH-M-1';

function row(seq: number, extra: Partial<V2xMessageRow> = {}): V2xMessageRow {
	return {
		key: KEY,
		seq,
		t_rx: 1760000000 + seq * 0.1,
		msg_type: 'BSM',
		source: 'tena',
		sender: KEY,
		uuid: `u${seq}`,
		raw_hex: '0014' + seq.toString(16).padStart(2, '0'),
		size: 3,
		summary: {
			id: '11111110',
			msg_cnt: 0,
			sec_mark: 1,
			lat: 0.0007912,
			lon: 0.0004081,
			elev_m: 6.2,
			speed_mps: 3.26,
			heading_deg: 201.9,
			brakes: false,
			transmission: 'forwardGears',
			size_m: null,
			lights: { siren: 'inUse', lights: 'inUse', multi: 'unavailable', role: 'ambulance', response_type: null, events: null },
			coords: 'origin-relative',
			emergency: true
		},
		error: null,
		tx: null,
		...extra
	};
}

const AMBULANCE: V2xVehicleEntry = {
	key: KEY,
	actor_id: 5,
	role: KEY,
	type_id: 'vehicle.ambulance.ford',
	kind: 'voices',
	subkind: 'scenario',
	org: 'PATH',
	bsm_id: '11111110',
	pos: [0, 0, 0],
	yaw: 0,
	speed_mps: 3.26,
	emergency: true,
	special_lights: true,
	feed: { source: 'tena', last_rx: 1, age_s: 0.1, rate_hz: 10, count: 5 },
	in_world: true
};

beforeEach(() => {
	resetV2xViewer();
	resetWindowManager();
});

describe('V2xStreamWindow', () => {
	it('renders the summary, the rows and the toolbar actions', async () => {
		openWindow(streamWindowId(KEY));
		v2xVehicles.set([AMBULANCE]);
		v2xStreams.set(
			new Map([[KEY, { key: KEY, rows: [row(1), row(2, { msg_type: 'SPaT', summary: { message_id: 19 } })], total: 2, paused: false, last: row(1), rateHz: 10 }]])
		);
		render(V2xStreamWindow, { props: { windowId: streamWindowId(KEY), key: KEY, onClose: () => {} } });

		const dialog = await screen.findByRole('dialog', { name: KEY });
		expect(dialog).toHaveTextContent('PATH · ambulance.ford · emergency vehicle');
		expect(screen.getByTestId('v2x-stream-summary')).toHaveTextContent('11.7 km/h');
		expect(screen.getByTestId('v2x-stream-summary')).toHaveTextContent('SIREN+LIGHTS');
		expect(screen.getAllByTestId('v2x-row')).toHaveLength(2);
		expect(dialog).toHaveTextContent('10 Hz');

		await fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
		expect(get(v2xStreams).get(KEY)?.paused).toBe(true);
		expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument();

		expect(screen.queryByTestId('v2x-row-raw')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Raw' }));
		expect(screen.getAllByTestId('v2x-row-raw')[0]).toHaveTextContent('001401');

		await fireEvent.change(screen.getByLabelText('Message type filter'), { target: { value: 'BSM' } });
		expect(screen.getAllByTestId('v2x-row')).toHaveLength(1);

		await fireEvent.click(screen.getAllByTestId('v2x-row')[0]);
		expect(screen.getByTestId('v2x-row-detail')).toHaveTextContent('"id": "11111110"');

		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		expect(get(v2xStreams).get(KEY)?.rows).toEqual([]);
		expect(screen.getByText('No messages yet.')).toBeInTheDocument();
	});
});
