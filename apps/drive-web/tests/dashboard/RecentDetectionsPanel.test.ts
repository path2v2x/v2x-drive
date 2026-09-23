import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { fetchDetectionHistory } = vi.hoisted(() => ({
	fetchDetectionHistory: vi.fn()
}));
vi.mock('$lib/api', () => ({ fetchDetectionHistory }));

import RecentDetectionsPanel from '$lib/components/RecentDetectionsPanel.svelte';
import type { DetectionHistoryPage, DetectionRecord } from '$lib/types';

function record(objectId: string, ts: string, overrides: Partial<DetectionRecord> = {}): DetectionRecord {
	return {
		ts,
		camera: 'ch1',
		object_id: objectId,
		object_type: 'car',
		confidence: 0.9,
		lat: 37.9156,
		lon: -122.3348,
		...overrides
	};
}

afterEach(() => {
	cleanup();
	fetchDetectionHistory.mockReset();
});

describe('RecentDetectionsPanel', () => {
	it('renders the newest records first with camera and position columns', async () => {
		fetchDetectionHistory.mockResolvedValue({
			items: [
				record('older', '2026-09-03T16:28:24.103Z'),
				record('newest', '2026-09-03T16:28:39.236Z', { camera: 'ch2', confidence: 0.9288 })
			],
			next: null
		});
		render(RecentDetectionsPanel, { props: { refreshMs: 60_000 } });
		await waitFor(() => expect(screen.getByText('newest')).toBeInTheDocument());

		const rows = screen.getAllByRole('row').slice(1);
		expect(rows[0]).toHaveTextContent('newest');
		expect(rows[0]).toHaveTextContent('ch2');
		expect(rows[0]).toHaveTextContent('0.93');
		expect(rows[0]).toHaveTextContent('37.915600, -122.334800');
		expect(rows[1]).toHaveTextContent('older');
	});

	it('does not let an older overlapping live poll overwrite a refresh', async () => {
		const first = Promise.withResolvers<DetectionHistoryPage>();
		const second = Promise.withResolvers<DetectionHistoryPage>();
		fetchDetectionHistory.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		render(RecentDetectionsPanel, { props: { refreshMs: 60_000 } });
		await waitFor(() => expect(fetchDetectionHistory).toHaveBeenCalledTimes(1));

		screen.getByRole('button', { name: 'Refresh' }).click();
		await waitFor(() => expect(fetchDetectionHistory).toHaveBeenCalledTimes(2));
		second.resolve({ items: [record('newest', new Date().toISOString())], next: null });
		await waitFor(() => expect(screen.getByText('newest')).toBeInTheDocument());

		first.resolve({ items: [record('older', new Date().toISOString())], next: null });
		await Promise.resolve();
		expect(screen.getByText('newest')).toBeInTheDocument();
		expect(screen.queryByText('older')).not.toBeInTheDocument();
	});

	it('invalidates the previous request when the selected range changes', async () => {
		const first = Promise.withResolvers<DetectionHistoryPage>();
		const second = Promise.withResolvers<DetectionHistoryPage>();
		fetchDetectionHistory.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		const view = render(RecentDetectionsPanel, {
			props: {
				refreshMs: 60_000,
				range: { start: '2026-07-10T00:00:00Z', end: '2026-07-10T00:10:00Z' }
			}
		});
		await waitFor(() => expect(fetchDetectionHistory).toHaveBeenCalledTimes(1));
		expect(fetchDetectionHistory).toHaveBeenLastCalledWith({
			start: '2026-07-10T00:00:00Z',
			end: '2026-07-10T00:10:00Z',
			limit: 25
		});

		await view.rerender({
			refreshMs: 60_000,
			range: { start: '2026-07-10T01:00:00Z', end: '2026-07-10T01:10:00Z' }
		});
		await waitFor(() => expect(fetchDetectionHistory).toHaveBeenCalledTimes(2));
		second.resolve({ items: [record('range-b', '2026-07-10T01:05:00Z')], next: null });
		await waitFor(() => expect(screen.getByText('range-b')).toBeInTheDocument());

		first.resolve({ items: [record('range-a', '2026-07-10T00:05:00Z')], next: null });
		await Promise.resolve();
		expect(screen.getByText('range-b')).toBeInTheDocument();
		expect(screen.queryByText('range-a')).not.toBeInTheDocument();
	});
});
