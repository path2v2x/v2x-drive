import { describe, expect, it, vi } from 'vitest';
import { playWithAbortRetry } from '$lib/media-play';

const abortError = Object.assign(new Error('The play() request was interrupted by a call to pause().'), {
	name: 'AbortError'
});
const notAllowed = Object.assign(new Error('play() failed because the user did not interact'), {
	name: 'NotAllowedError'
});

describe('playWithAbortRetry', () => {
	it('resolves on first success without retrying', async () => {
		const play = vi.fn().mockResolvedValue(undefined);
		await playWithAbortRetry({ play }, 0);
		expect(play).toHaveBeenCalledTimes(1);
	});

	it('retries exactly once after an AbortError and succeeds', async () => {
		const play = vi.fn().mockRejectedValueOnce(abortError).mockResolvedValueOnce(undefined);
		await playWithAbortRetry({ play }, 0);
		expect(play).toHaveBeenCalledTimes(2);
	});

	it('propagates a second AbortError instead of looping', async () => {
		const play = vi.fn().mockRejectedValue(abortError);
		await expect(playWithAbortRetry({ play }, 0)).rejects.toBe(abortError);
		expect(play).toHaveBeenCalledTimes(2);
	});

	it('does not retry non-abort failures', async () => {
		const play = vi.fn().mockRejectedValue(notAllowed);
		await expect(playWithAbortRetry({ play }, 0)).rejects.toBe(notAllowed);
		expect(play).toHaveBeenCalledTimes(1);
	});

	it('does not retry a plain string rejection', async () => {
		const play = vi.fn().mockRejectedValue('AbortError');
		await expect(playWithAbortRetry({ play }, 0)).rejects.toBe('AbortError');
		expect(play).toHaveBeenCalledTimes(1);
	});
});
