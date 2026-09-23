import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';

const apiMocks = vi.hoisted(() => ({
	listArchiveSegments: vi.fn()
}));

vi.mock('$lib/api', () => ({
	archiveClipUrl: vi.fn(),
	listArchiveSegments: apiMocks.listArchiveSegments
}));

import ArchiveVideoCard from '$lib/components/ArchiveVideoCard.svelte';

beforeEach(() => {
	vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
	vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
	apiMocks.listArchiveSegments.mockReset();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

describe('ArchiveVideoCard configuration', () => {
	it('reports an unconfigured source without requesting an archive session', async () => {
		render(ArchiveVideoCard, {
			props: {
				cameraId: 'ch1',
				windowStart: '2026-09-02T00:00:00.000Z',
				windowEnd: '2026-09-02T00:15:00.000Z',
				windowStartMs: Date.parse('2026-09-02T00:00:00.000Z'),
				archiveVideoBaseUrl: '',
				cursorMs: Date.parse('2026-09-02T00:00:10.000Z'),
				seekNonce: 0,
				playing: false
			}
		});

		await waitFor(() =>
			expect(screen.getByText('Video source not configured')).toBeInTheDocument()
		);
		expect(apiMocks.listArchiveSegments).not.toHaveBeenCalled();
	});
});
