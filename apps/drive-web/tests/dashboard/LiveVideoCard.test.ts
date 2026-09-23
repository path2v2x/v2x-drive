import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';

const hlsMocks = vi.hoisted(() => ({
	attachMedia: vi.fn(),
	destroy: vi.fn(),
	loadSource: vi.fn()
}));


vi.mock('hls.js', () => ({
	default: class MockHls {
		static isSupported() {
			return true;
		}

		attachMedia = hlsMocks.attachMedia;
		destroy = hlsMocks.destroy;
		loadSource = hlsMocks.loadSource;
	}
}));


import LiveVideoCard from '$lib/components/LiveVideoCard.svelte';

beforeEach(() => {
	vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
	vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
	vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
	hlsMocks.attachMedia.mockReset();
	hlsMocks.destroy.mockReset();
	hlsMocks.loadSource.mockReset();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

describe('LiveVideoCard camera selection', () => {
	it('reconnects the image stream when the selected camera changes', async () => {
		const view = render(LiveVideoCard, {
			props: {
				cameraId: 'ch1',
				streamUrl: 'https://perception.example.test/streams/ch1.mjpg',
				liveVideoUrlTemplate: '',
				sourceLabel: 'Perception'
			}
		});

		await waitFor(() =>
			expect(screen.getByRole('img', { name: 'ch1 perception stream' })).toHaveAttribute(
				'src',
				'https://perception.example.test/streams/ch1.mjpg'
			)
		);

		await view.rerender({
			cameraId: 'ch4',
			streamUrl: 'https://perception.example.test/streams/ch4.mjpg',
			liveVideoUrlTemplate: '',
			sourceLabel: 'Perception'
		});

		await waitFor(() =>
			expect(screen.getByRole('img', { name: 'ch4 perception stream' })).toHaveAttribute(
				'src',
				'https://perception.example.test/streams/ch4.mjpg'
			)
		);
		expect(screen.queryByRole('img', { name: 'ch1 perception stream' })).not.toBeInTheDocument();
	});

	it('mounts a video element before switching from MJPEG to HLS', async () => {
		const view = render(LiveVideoCard, {
			props: {
				cameraId: 'ch1',
				liveVideoUrlTemplate: '',
				streamUrl: 'https://perception.example.test/streams/ch1.mjpg'
			}
		});
		await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument());

		await view.rerender({
			cameraId: 'ch4',
			liveVideoUrlTemplate: '',
			streamUrl: 'https://video.example.test/streams/ch4.m3u8'
		});

		await waitFor(() =>
			expect(hlsMocks.loadSource).toHaveBeenCalledWith(
				'https://video.example.test/streams/ch4.m3u8'
			)
		);
		expect(hlsMocks.attachMedia).toHaveBeenCalledWith(expect.any(HTMLVideoElement));
		expect(screen.queryByRole('img')).not.toBeInTheDocument();
	});

	it('prefers hls.js when Chromium also claims native HLS support', async () => {
		vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably');

		render(LiveVideoCard, {
			props: {
				cameraId: 'ch2',
				liveVideoUrlTemplate: '',
				streamUrl: 'https://video.example.test/streams/ch2.m3u8'
			}
		});

		await waitFor(() =>
			expect(hlsMocks.loadSource).toHaveBeenCalledWith(
				'https://video.example.test/streams/ch2.m3u8'
			)
		);
		expect(hlsMocks.attachMedia).toHaveBeenCalledWith(expect.any(HTMLVideoElement));
	});

	it('resolves a configured live URL', async () => {
		render(LiveVideoCard, {
			props: {
				cameraId: 'ch 2',
				liveVideoUrlTemplate:
					'https://drive.example.test/camera/{camera_id}/index.m3u8'
			}
		});

		await waitFor(() =>
			expect(hlsMocks.loadSource).toHaveBeenCalledWith(
				'https://drive.example.test/camera/ch%202/index.m3u8'
			)
		);
	});

	it('reports an unconfigured source without requesting a session fallback', async () => {
		render(LiveVideoCard, {
			props: {
				cameraId: 'ch1',
				liveVideoUrlTemplate: ''
			}
		});

		await waitFor(() =>
			expect(screen.getByText('Video source not configured')).toBeInTheDocument()
		);
		expect(hlsMocks.loadSource).not.toHaveBeenCalled();
	});
});
