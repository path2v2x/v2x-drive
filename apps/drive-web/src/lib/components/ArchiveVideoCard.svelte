<script lang="ts">
	import { onDestroy } from 'svelte';
	import { archiveClipUrl, listArchiveSegments } from '$lib/api';
	import {
		archiveCursorNeedsCorrection,
		archiveMediaTimeForEpoch,
		archiveEpochForMediaTime,
		formatClock
	} from '$lib/timeline';

	interface Props {
		cameraId: string;
		/** ISO window bounds; changing them loads a fresh archive clip. */
		windowStart: string;
		windowEnd: string;
		windowStartMs: number;
		archiveVideoBaseUrl: string;
		/** Target wall-clock position; bump seekNonce to force a seek. */
		cursorMs: number;
		seekNonce: number;
		playing: boolean;
		/** Only the primary card reports time back to the page. */
		isPrimary?: boolean;
		onTimeUpdate?: (epochMs: number) => void;
	}

	let {
		cameraId,
		windowStart,
		windowEnd,
		windowStartMs,
		archiveVideoBaseUrl,
		cursorMs,
		seekNonce,
		playing,
		isPrimary = false,
		onTimeUpdate
	}: Props = $props();

	let videoEl = $state<HTMLVideoElement | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let noRecording = $state(false);
	let currentEpochMs = $state<number | null>(null);
	let loadedWindowKey = '';
	let requestSerial = 0;
	// Local MP4 media time is relative to the first recording in the clip.
	let localClipStartMs: number | null = null;
	let appliedSeekNonce = -1;

	function destroyPlayer() {
		if (videoEl) {
			videoEl.pause();
			videoEl.removeAttribute('src');
			videoEl.load();
		}
		localClipStartMs = null;
		currentEpochMs = null;
	}

	function mediaTimeForEpoch(epochMs: number): number {
		return archiveMediaTimeForEpoch(epochMs, localClipStartMs ?? windowStartMs);
	}

	async function loadLocalArchive(serial: number) {
		const segments = await listArchiveSegments(cameraId, windowStart, windowEnd);
		if (serial !== requestSerial || !videoEl) return;

		const requestedEndMs = Date.parse(windowEnd);
		const overlapping = segments
			.map((segment) => {
				const startMs = Date.parse(segment.start);
				return { startMs, endMs: startMs + segment.duration * 1000 };
			})
			.filter(
				(segment) => segment.endMs > windowStartMs && segment.startMs < requestedEndMs
			);
		if (overlapping.length === 0) {
			noRecording = true;
			return;
		}

		const clipStartMs = Math.max(
			windowStartMs,
			Math.min(...overlapping.map((segment) => segment.startMs))
		);
		const clipEndMs = Math.min(
			requestedEndMs,
			Math.max(...overlapping.map((segment) => segment.endMs))
		);
		if (clipEndMs <= clipStartMs) {
			noRecording = true;
			return;
		}

		localClipStartMs = clipStartMs;
		videoEl.src = archiveClipUrl(
			archiveVideoBaseUrl,
			cameraId,
			new Date(clipStartMs).toISOString(),
			(clipEndMs - clipStartMs) / 1000
		);
		videoEl.load();
	}


	async function loadWindow() {
		const key = `${cameraId}|${windowStart}|${windowEnd}|${archiveVideoBaseUrl}`;
		if (key === loadedWindowKey) return;
		const serial = ++requestSerial;
		loading = true;
		error = null;
		noRecording = false;
		destroyPlayer();
		loadedWindowKey = key;

		try {
			if (!archiveVideoBaseUrl.trim()) {
				throw new Error('Video source not configured');
			}
			await loadLocalArchive(serial);
			if (serial !== requestSerial || !videoEl || noRecording) return;
			videoEl.currentTime = mediaTimeForEpoch(cursorMs);
			if (playing) {
				await videoEl.play().catch(() => {});
			}
		} catch (err) {
			if (serial !== requestSerial) return;
			error = err instanceof Error ? err.message : 'Unknown playback error';
			loadedWindowKey = '';
		} finally {
			if (serial === requestSerial) loading = false;
		}
	}

	function handleLoadedMetadata() {
		if (!videoEl) return;
		videoEl.currentTime = mediaTimeForEpoch(cursorMs);
		if (playing) void videoEl.play().catch(() => {});
	}

	function handleTimeUpdate() {
		if (!videoEl) return;
		currentEpochMs = archiveEpochForMediaTime(
			localClipStartMs ?? windowStartMs,
			videoEl.currentTime
		);
		if (isPrimary) {
			onTimeUpdate?.(currentEpochMs);
		}
	}

	$effect(() => {
		void windowStart;
		void windowEnd;
		void archiveVideoBaseUrl;
		if (videoEl) {
			void loadWindow();
		}
	});

	$effect(() => {
		if (seekNonce === appliedSeekNonce || !videoEl || noRecording) return;
		appliedSeekNonce = seekNonce;
		videoEl.currentTime = mediaTimeForEpoch(cursorMs);
	});

	// Followers drift-correct against the shared cursor instead of emitting time.
	$effect(() => {
		if (isPrimary || !videoEl || currentEpochMs === null || noRecording) return;
		if (archiveCursorNeedsCorrection(cursorMs, currentEpochMs)) {
			videoEl.currentTime = mediaTimeForEpoch(cursorMs);
		}
	});

	$effect(() => {
		if (!videoEl || noRecording) return;
		if (playing) {
			void videoEl.play().catch(() => {});
		} else {
			videoEl.pause();
		}
	});

	onDestroy(() => {
		requestSerial += 1;
		destroyPlayer();
	});
</script>

<div class="relative overflow-hidden border border-gray-900 bg-black" style="aspect-ratio: 4 / 3;">
	<div class="absolute top-2 left-2 z-10 bg-black/70 px-2 py-1 text-[10px] font-medium tracking-[0.18em] text-gray-200 uppercase">
		{cameraId}
	</div>

	<div class="absolute top-2 right-2 z-10 flex items-center gap-2">
		<span class="bg-amber-500/90 px-2 py-1 text-[10px] font-semibold tracking-[0.16em] text-black uppercase">
			Archive
		</span>
		{#if currentEpochMs}
			<span class="bg-black/70 px-2 py-1 font-mono text-[10px] text-gray-300">
				{formatClock(currentEpochMs)}
			</span>
		{/if}
	</div>

	<video
		bind:this={videoEl}
		class="h-full w-full object-cover"
		playsinline
		muted
		onloadedmetadata={handleLoadedMetadata}
		ontimeupdate={handleTimeUpdate}
	></video>

	{#if noRecording}
		<div class="absolute inset-0 flex items-center justify-center bg-black/75 px-4 text-center text-sm text-gray-300">
			No recording for this window
		</div>
	{/if}

	{#if loading}
		<div class="absolute inset-0 flex items-center justify-center bg-black/45">
			<div class="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-amber-300"></div>
		</div>
	{/if}

	{#if error}
		<div class="absolute right-0 bottom-0 left-0 z-10 flex items-center justify-between gap-2 bg-black/85 px-3 py-2 text-[11px] text-rose-300">
			<span>{error}</span>
			<button
				class="border border-gray-600 px-2 py-1 text-[10px] tracking-[0.14em] text-white uppercase hover:border-gray-400"
				onclick={() => {
					loadedWindowKey = '';
					void loadWindow();
				}}
			>
				Retry
			</button>
		</div>
	{/if}
</div>
