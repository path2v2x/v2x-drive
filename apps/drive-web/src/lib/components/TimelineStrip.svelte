<script lang="ts">
	import type { CoverageBucket, DetectionObject } from '$lib/types';
	import {
		TIMELINE_SPAN_MS,
		formatClock,
		layoutHistogram,
		layoutMarkers,
		objectTypeColor,
		timeTicks
	} from '$lib/timeline';

	interface Props {
		viewStartMs: number;
		viewEndMs: number;
		cursorMs: number;
		liveEdgeMs: number;
		events: DetectionObject[];
		histogram: CoverageBucket[];
		bucketSeconds: number;
		selectedObjectId: string | null;
		onScrub: (epochMs: number) => void;
		onSelectEvent: (event: DetectionObject) => void;
		onViewChange: (viewStartMs: number, viewEndMs: number) => void;
	}

	let {
		viewStartMs,
		viewEndMs,
		cursorMs,
		liveEdgeMs,
		events,
		histogram,
		bucketSeconds,
		selectedObjectId,
		onScrub,
		onSelectEvent,
		onViewChange
	}: Props = $props();

	let trackEl = $state<HTMLDivElement | null>(null);
	let scrubbing = $state(false);
	let hovered = $state<{ event: DetectionObject; clientX: number } | null>(null);

	const MIN_SPAN_MS = 2 * 60 * 1000;
	const MAX_SPAN_MS = TIMELINE_SPAN_MS;

	let markers = $derived(layoutMarkers(events, viewStartMs, viewEndMs));
	let histogramBars = $derived(
		layoutHistogram(histogram, bucketSeconds, viewStartMs, viewEndMs)
	);
	let ticks = $derived(timeTicks(viewStartMs, viewEndMs));
	let cursorX = $derived(
		Math.min(Math.max((cursorMs - viewStartMs) / (viewEndMs - viewStartMs), 0), 1)
	);
	let cursorVisible = $derived(cursorMs >= viewStartMs && cursorMs <= viewEndMs);

	function epochAtClientX(clientX: number): number {
		if (!trackEl) return viewStartMs;
		const rect = trackEl.getBoundingClientRect();
		const fraction = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
		return viewStartMs + fraction * (viewEndMs - viewStartMs);
	}

	function handlePointerDown(event: PointerEvent) {
		scrubbing = true;
		trackEl?.setPointerCapture(event.pointerId);
		onScrub(Math.min(epochAtClientX(event.clientX), liveEdgeMs));
	}

	function handlePointerMove(event: PointerEvent) {
		if (!scrubbing) return;
		onScrub(Math.min(epochAtClientX(event.clientX), liveEdgeMs));
	}

	function handlePointerUp(event: PointerEvent) {
		scrubbing = false;
		trackEl?.releasePointerCapture(event.pointerId);
	}

	function handleWheel(event: WheelEvent) {
		event.preventDefault();
		const anchor = epochAtClientX(event.clientX);
		const span = viewEndMs - viewStartMs;
		const factor = event.deltaY > 0 ? 1.25 : 0.8;
		const newSpan = Math.min(Math.max(span * factor, MIN_SPAN_MS), MAX_SPAN_MS);
		const anchorFraction = (anchor - viewStartMs) / span;
		let newStart = anchor - anchorFraction * newSpan;
		let newEnd = newStart + newSpan;
		if (newEnd > liveEdgeMs) {
			newEnd = liveEdgeMs;
			newStart = newEnd - newSpan;
		}
		onViewChange(newStart, newEnd);
	}

	function setSpan(spanMs: number) {
		const end = Math.min(Math.max(cursorMs + spanMs / 2, viewStartMs + spanMs), liveEdgeMs);
		onViewChange(end - spanMs, end);
	}

	const spanPresets = [
		{ label: '72h', ms: 72 * 60 * 60 * 1000 },
		{ label: '24h', ms: 24 * 60 * 60 * 1000 },
		{ label: '6h', ms: 6 * 60 * 60 * 1000 },
		{ label: '1h', ms: 60 * 60 * 1000 },
		{ label: '10m', ms: 10 * 60 * 1000 }
	];

	let legendTypes = $derived(
		[...new Set(events.map((e) => e.object_type))].sort()
	);
</script>

<div class="flex flex-col gap-1.5 border border-gray-800 bg-gray-950 px-4 py-3 select-none">
	<div class="flex items-center justify-between gap-3">
		<div class="flex items-center gap-3">
			<span class="text-[11px] font-semibold tracking-[0.18em] text-gray-300 uppercase">Timeline</span>
			<span class="font-mono text-[11px] text-amber-300">{formatClock(cursorMs)}</span>
		</div>
		<div class="flex items-center gap-3">
			<div class="flex items-center gap-2">
				{#each legendTypes as objectType}
					<span class="flex items-center gap-1 text-[10px] text-gray-400">
						<span class="h-2 w-2 rounded-full" style={`background:${objectTypeColor(objectType)}`}></span>
						{objectType}
					</span>
				{/each}
			</div>
			<div class="flex items-center gap-1">
				{#each spanPresets as preset}
					<button
						class="border border-gray-700 bg-gray-900 px-2 py-0.5 text-[10px] tracking-[0.12em] text-gray-300 uppercase hover:border-gray-500 hover:text-white"
						onclick={() => setSpan(preset.ms)}
					>
						{preset.label}
					</button>
				{/each}
			</div>
		</div>
	</div>

	<div
		bind:this={trackEl}
		class="relative h-20 cursor-crosshair overflow-hidden bg-black"
		role="slider"
		tabindex="0"
		aria-label="Timeline scrubber"
		aria-valuemin={viewStartMs}
		aria-valuemax={viewEndMs}
		aria-valuenow={cursorMs}
		onpointerdown={handlePointerDown}
		onpointermove={handlePointerMove}
		onpointerup={handlePointerUp}
		onwheel={handleWheel}
	>

		<!-- Detection density -->
		<div class="absolute inset-x-0 top-0 bottom-6">
			{#each histogramBars as bar}
				<div
					class="absolute bottom-0"
					style={`left:${bar.x * 100}%;width:${Math.max(bar.width * 100, 0.08)}%;height:${Math.max(bar.intensity * 100, 4)}%;background:rgba(56,189,248,${0.12 + bar.intensity * 0.45})`}
				></div>
			{/each}
		</div>

		<!-- Event markers: first appearance of each track -->
		{#each markers as marker}
			<button
				class="absolute top-0 bottom-6 w-[3px] -translate-x-1/2 opacity-80 hover:opacity-100"
				class:!w-[5px]={marker.event.object_id === selectedObjectId}
				style={`left:${marker.x * 100}%;background:${marker.color}`}
				aria-label={`${marker.event.object_type} ${marker.event.object_id}`}
				onpointerdown={(e) => e.stopPropagation()}
				onclick={(e) => {
					e.stopPropagation();
					onSelectEvent(marker.event);
				}}
				onmouseenter={(e) => (hovered = { event: marker.event, clientX: e.clientX })}
				onmouseleave={() => (hovered = null)}
			></button>
		{/each}

		<!-- Time ticks -->
		<div class="absolute inset-x-0 bottom-0 h-6 border-t border-gray-800/80">
			{#each ticks as tick}
				<div class="absolute bottom-0 h-full" style={`left:${tick.x * 100}%`}>
					<div class="h-1.5 w-px bg-gray-600"></div>
					<span class="block -translate-x-1/2 font-mono text-[9px] text-gray-500">{tick.label}</span>
				</div>
			{/each}
		</div>

		<!-- Cursor -->
		{#if cursorVisible}
			<div
				class="pointer-events-none absolute top-0 bottom-0 w-px bg-amber-300"
				style={`left:${cursorX * 100}%`}
			>
				<div class="absolute top-0 left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 bg-amber-300"></div>
			</div>
		{/if}
	</div>

	{#if hovered}
		<div class="flex items-center gap-3 font-mono text-[11px] text-gray-300">
			<span class="h-2 w-2 rounded-full" style={`background:${objectTypeColor(hovered.event.object_type)}`}></span>
			<span>{hovered.event.object_id}</span>
			<span class="text-gray-500">{hovered.event.cameras.join(', ')}</span>
			<span>{formatClock(Date.parse(hovered.event.first_seen))}</span>
			<span class="text-gray-500">conf {hovered.event.max_confidence.toFixed(2)}</span>
			<span class="text-gray-500">{hovered.event.count} detections</span>
		</div>
	{:else}
		<p class="text-[11px] text-gray-600">
			Drag to scrub &middot; wheel to zoom &middot; click a marker to jump to that event
		</p>
	{/if}
</div>
