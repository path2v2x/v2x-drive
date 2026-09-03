<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import Header from '$lib/components/Header.svelte';
	import ArchiveVideoCard from '$lib/components/ArchiveVideoCard.svelte';
	import LiveVideoCard from '$lib/components/LiveVideoCard.svelte';
	import TimelineStrip from '$lib/components/TimelineStrip.svelte';
	import RecentDetectionsPanel from '$lib/components/RecentDetectionsPanel.svelte';
	import { fetchDetectionCoverage, fetchDetectionObjects } from '$lib/api';
	import { loadRuntimeConfig, type RuntimeConfig } from '$lib/runtime-config';
	import {
		TIMELINE_SPAN_MS,
		coverageBucketSecondsForSpan,
		parseIsoMs,
		toIsoMillis,
		windowForCursor,
		type PlaybackWindow
	} from '$lib/timeline';
	import type { DetectionCoverage, DetectionObject } from '$lib/types';

	const DEFAULT_VIEW_SPAN_MS = 24 * 60 * 60 * 1000;

	let runtimeConfig = $state<RuntimeConfig | null>(null);
	let mode = $state<'live' | 'archive'>('live');
	let nowMs = $state(Date.now());
	let cursorMs = $state(Date.now());
	let viewStartMs = $state(Date.now() - DEFAULT_VIEW_SPAN_MS);
	let viewEndMs = $state(Date.now());
	let playing = $state(true);
	let playbackWindow = $state<PlaybackWindow | null>(null);
	let seekNonce = $state(0);
	let primaryCameraId = $state('ch1');
	let selectedObjectId = $state<string | null>(null);
	let events = $state<DetectionObject[]>([]);
	let coverage = $state<DetectionCoverage | null>(null);
	let timelineError = $state<string | null>(null);

	let cameraIds = $derived(runtimeConfig?.videoCameraIds ?? ['ch1', 'ch2', 'ch3', 'ch4']);
	// Quantised to 10s steps so playback doesn't re-query the DB on every tick.
	let dbRange = $derived.by(() => {
		if (mode !== 'archive') return null;
		const quantised = Math.floor(cursorMs / 10_000) * 10_000;
		return {
			start: toIsoMillis(quantised - 30_000),
			end: toIsoMillis(quantised + 30_000)
		};
	});

	let clockTimer: ReturnType<typeof setInterval> | null = null;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;
	let coverageTimer: ReturnType<typeof setTimeout> | null = null;
	let coverageSerial = 0;

	async function loadEvents() {
		try {
			const end = Date.now();
			events = await fetchDetectionObjects({
				start: toIsoMillis(end - TIMELINE_SPAN_MS),
				end: toIsoMillis(end),
				limit: 1000
			});
			timelineError = null;
		} catch (err) {
			timelineError = err instanceof Error ? err.message : 'Failed to load detections.';
		}
	}

	/** Coverage follows the visible span so zooming in keeps the histogram fine-grained. */
	async function loadCoverage() {
		const serial = ++coverageSerial;
		const start = viewStartMs;
		const end = Math.max(viewEndMs, start + 60_000);
		try {
			const result = await fetchDetectionCoverage({
				start: toIsoMillis(start),
				end: toIsoMillis(end),
				bucketSeconds: coverageBucketSecondsForSpan(end - start)
			});
			if (serial !== coverageSerial) return;
			coverage = result;
		} catch (err) {
			if (serial !== coverageSerial) return;
			timelineError = err instanceof Error ? err.message : 'Failed to load detection coverage.';
		}
	}

	function scheduleCoverage() {
		if (coverageTimer) clearTimeout(coverageTimer);
		coverageTimer = setTimeout(() => {
			coverageTimer = null;
			void loadCoverage();
		}, 250);
	}


	function goLive() {
		mode = 'live';
		playing = true;
		selectedObjectId = null;
		cursorMs = Date.now();
	}

	function scrubTo(epochMs: number) {
		const now = Date.now();
		nowMs = now;
		// Scrubbing to (or past) the live edge returns to live mode.
		if (now - epochMs < 20_000) {
			goLive();
			return;
		}
		mode = 'archive';
		cursorMs = epochMs;
		const win = windowForCursor(epochMs, now);
		if (!playbackWindow || win.start !== playbackWindow.start || win.end !== playbackWindow.end) {
			playbackWindow = win;
		}
		seekNonce += 1;
	}

	function handleSelectEvent(event: DetectionObject) {
		selectedObjectId = event.object_id;
		const firstSeen = parseIsoMs(event.first_seen);
		if (firstSeen !== null) {
			scrubTo(Math.max(firstSeen - 10_000, Date.now() - TIMELINE_SPAN_MS));
		}
		const camera = event.cameras.find((id) => cameraIds.includes(id));
		if (camera) primaryCameraId = camera;
	}

	function handleViewChange(startMs: number, endMs: number) {
		viewStartMs = startMs;
		viewEndMs = endMs;
		scheduleCoverage();
	}

	function handlePrimaryTime(epochMs: number) {
		cursorMs = epochMs;
		// Roll into the next playback window as playback approaches the edge.
		if (playbackWindow && epochMs >= playbackWindow.endMs - 1_000) {
			scrubTo(epochMs + 2_000);
		}
	}

	onMount(async () => {
		runtimeConfig = await loadRuntimeConfig();
		const now = Date.now();
		nowMs = now;
		cursorMs = now;
		viewStartMs = now - DEFAULT_VIEW_SPAN_MS;
		viewEndMs = now;

		void loadEvents();
		void loadCoverage();

		clockTimer = setInterval(() => {
			nowMs = Date.now();
			if (mode === 'live') {
				cursorMs = nowMs;
				viewEndMs = nowMs;
				viewStartMs = Math.max(viewStartMs, nowMs - TIMELINE_SPAN_MS);
			}
		}, 1000);
		refreshTimer = setInterval(() => {
			void loadEvents();
			void loadCoverage();
		}, 60_000);
	});

	onDestroy(() => {
		if (clockTimer) clearInterval(clockTimer);
		if (refreshTimer) clearInterval(refreshTimer);
		if (coverageTimer) clearTimeout(coverageTimer);
	});
</script>

<svelte:head>
	<title>V2X Street Camera Timeline</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950">
	<Header />

	<div class="min-h-0 flex-1 overflow-y-auto bg-black">
		<!-- Camera grid -->
		<div
			class="grid gap-px bg-gray-900"
			style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));"
		>
			{#each cameraIds as cameraId}
				<div
					class={`relative ${cameraId === primaryCameraId ? 'ring-1 ring-amber-400/60 ring-inset' : ''}`}
					role="button"
					tabindex="0"
					onclick={() => (primaryCameraId = cameraId)}
					onkeydown={(e) => e.key === 'Enter' && (primaryCameraId = cameraId)}
				>
					{#if mode === 'archive' && playbackWindow}
						<ArchiveVideoCard
							{cameraId}
							windowStart={playbackWindow.start}
							windowEnd={playbackWindow.end}
							windowStartMs={playbackWindow.startMs}
							archiveVideoBaseUrl={runtimeConfig?.archiveVideoBaseUrl || ''}
							{cursorMs}
							{seekNonce}
							{playing}
							isPrimary={cameraId === primaryCameraId}
							onTimeUpdate={handlePrimaryTime}
						/>
					{:else}
						<LiveVideoCard
							{cameraId}
							liveVideoUrlTemplate={runtimeConfig?.liveVideoUrlTemplate || ''}
						/>
					{/if}
				</div>
			{/each}
		</div>

		<!-- Controls + timeline -->
		<div class="flex flex-col gap-2 px-4 py-3">
			<div class="flex items-center gap-2">
				<button
					class={`border px-3 py-1.5 text-[11px] font-semibold tracking-[0.16em] uppercase transition ${
						mode === 'live'
							? 'border-emerald-400/60 bg-emerald-400/10 text-emerald-200'
							: 'border-gray-700 bg-gray-900 text-gray-300 hover:border-gray-500 hover:text-white'
					}`}
					onclick={goLive}
				>
					Live
				</button>
				{#if mode === 'archive'}
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] font-semibold tracking-[0.16em] text-gray-200 uppercase hover:border-gray-500"
						onclick={() => (playing = !playing)}
					>
						{playing ? 'Pause' : 'Play'}
					</button>
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] tracking-[0.16em] text-gray-300 uppercase hover:border-gray-500 hover:text-white"
						onclick={() => scrubTo(cursorMs - 30_000)}
					>
						-30s
					</button>
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] tracking-[0.16em] text-gray-300 uppercase hover:border-gray-500 hover:text-white"
						onclick={() => scrubTo(cursorMs + 30_000)}
					>
						+30s
					</button>
				{/if}
				<span class="ml-auto text-[11px] text-gray-500">
					{events.length} objects in the past 72h
					{#if coverage}
						/ {coverage.buckets.reduce((sum, bucket) => sum + bucket.detections, 0)} detections in view
					{/if}
				</span>
			</div>

			{#if timelineError}
				<p class="text-[11px] text-rose-300">{timelineError}</p>
			{/if}

			<TimelineStrip
				{viewStartMs}
				{viewEndMs}
				{cursorMs}
				liveEdgeMs={nowMs}
				{events}
				histogram={coverage?.buckets ?? []}
				bucketSeconds={coverage?.bucket_seconds ?? 300}
				{selectedObjectId}
				onScrub={scrubTo}
				onSelectEvent={handleSelectEvent}
				onViewChange={handleViewChange}
			/>
		</div>

		<!-- Objects DB: live-polling, or time-locked to the scrub cursor -->
		<RecentDetectionsPanel limit={50} range={dbRange} highlightObjectId={selectedObjectId} />
	</div>
</div>
