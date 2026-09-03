<script lang="ts">
	import { fetchDetectionHistory } from '$lib/api';
	import type { DetectionRecord } from '$lib/types';
	import { toIsoMillis } from '$lib/timeline';
	import {
		isProducerTimestampFresh,
		latestProducerTimestamp
	} from '$lib/producer-time';

	const RECENT_DETECTIONS_STALE_AFTER_MS = 30_000;
	/** Live mode shows everything recorded in the last minute. */
	const LIVE_WINDOW_MS = 60_000;

	interface Props {
		limit?: number;
		refreshMs?: number;
		/** When set, shows detections in this window instead of live-polling. */
		range?: { start: string; end: string } | null;
		highlightObjectId?: string | null;
	}

	let { limit = 25, refreshMs = 5000, range = null, highlightObjectId = null }: Props = $props();

	let items = $state<DetectionRecord[]>([]);
	let isLoading = $state(false);
	let error = $state<string | null>(null);
	let latestProducerAt = $state<string | null>(null);
	let evaluatedAtMs = $state(Date.now());
	let refreshTimer: ReturnType<typeof setInterval> | null = null;
	let queryGeneration = 0;
	let latestRequestId = 0;

	type DetectionQuery =
		| { mode: 'range'; start: string; end: string; limit: number }
		| { mode: 'recent'; limit: number };

	function displayTime(value: string): string {
		const ms = Date.parse(value);
		return Number.isFinite(ms) ? new Date(ms).toLocaleTimeString([], { hour12: false }) : value;
	}

	function displayPosition(item: DetectionRecord): string {
		return `${item.lat.toFixed(6)}, ${item.lon.toFixed(6)}`;
	}

	function currentQuery(): DetectionQuery {
		return range
			? { mode: 'range', start: range.start, end: range.end, limit }
			: { mode: 'recent', limit };
	}

	/** Newest-first rows for the table; the API returns ascending order. */
	function newestFirst(records: DetectionRecord[]): DetectionRecord[] {
		return [...records].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
	}

	function invalidateRequests() {
		queryGeneration += 1;
		latestRequestId += 1;
	}

	async function loadItemsFor(
		query: DetectionQuery,
		generation: number,
		showLoading: boolean
	) {
		if (generation !== queryGeneration) return;
		const requestId = ++latestRequestId;
		evaluatedAtMs = Date.now();
		error = null;
		isLoading = showLoading;

		try {
			const window =
				query.mode === 'range'
					? { start: query.start, end: query.end }
					: { start: toIsoMillis(Date.now() - LIVE_WINDOW_MS), end: toIsoMillis(Date.now() + 5_000) };
			const response = await fetchDetectionHistory({
				start: window.start,
				end: window.end,
				limit: query.mode === 'recent' ? 5000 : query.limit
			});
			if (generation !== queryGeneration || requestId !== latestRequestId) return;
			evaluatedAtMs = Date.now();
			items = newestFirst(response.items).slice(0, query.limit);
			latestProducerAt = latestProducerTimestamp(items.map((item) => item.ts));
		} catch (err) {
			if (generation !== queryGeneration || requestId !== latestRequestId) return;
			error = err instanceof Error ? err.message : 'Failed to fetch detections.';
		} finally {
			if (generation === queryGeneration && requestId === latestRequestId) {
				isLoading = false;
			}
		}
	}

	function loadItems() {
		return loadItemsFor(currentQuery(), queryGeneration, items.length === 0);
	}

	let producerCurrent = $derived(
		isProducerTimestampFresh(
			latestProducerAt,
			RECENT_DETECTIONS_STALE_AFTER_MS,
			evaluatedAtMs
		)
	);

	function producerTime(value: string | null): string | null {
		if (!value) return null;
		return new Date(value).toLocaleString();
	}

	function stopPolling() {
		if (refreshTimer) {
			clearInterval(refreshTimer);
			refreshTimer = null;
		}
	}

	$effect(() => {
		stopPolling();
		const activeRange = range;
		const activeLimit = limit;
		const activeRefreshMs = refreshMs;
		const query: DetectionQuery = activeRange
			? {
					mode: 'range',
					start: activeRange.start,
					end: activeRange.end,
					limit: activeLimit
				}
			: { mode: 'recent', limit: activeLimit };
		invalidateRequests();
		const generation = queryGeneration;
		items = [];
		latestProducerAt = null;
		error = null;
		isLoading = true;
		evaluatedAtMs = Date.now();
		void loadItemsFor(query, generation, true);
		if (query.mode === 'recent') {
			refreshTimer = setInterval(() => {
				void loadItemsFor(query, generation, false);
			}, activeRefreshMs);
		}

		return () => {
			stopPolling();
			if (queryGeneration === generation) invalidateRequests();
		};
	});
</script>

<section class="border-t border-gray-800 bg-gray-950">
	<div class="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-4">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<h1 class="text-lg font-semibold text-white">
					Objects DB
					{#if range}
						<span class="ml-2 align-middle text-[10px] font-medium tracking-[0.16em] text-amber-300 uppercase">Time-locked</span>
					{:else}
						<span class="ml-2 align-middle text-[10px] font-medium tracking-[0.16em] {producerCurrent ? 'text-emerald-300' : 'text-amber-300'} uppercase">
							{producerCurrent ? 'Current' : 'Stale'}
						</span>
					{/if}
				</h1>
				{#if producerTime(latestProducerAt)}
					<p class="mt-1 text-xs text-gray-500">
						Latest producer event {producerTime(latestProducerAt)}
					</p>
				{/if}
			</div>
			<button
				class="border border-gray-700 bg-gray-900 px-3 py-2 text-xs font-medium tracking-[0.14em] text-gray-200 uppercase transition hover:border-gray-500 hover:text-white"
				onclick={loadItems}
			>
				Refresh
			</button>
		</div>

		{#if error}
			<p class="text-sm text-red-300">{error}</p>
		{/if}

		<div class="overflow-auto border border-gray-800">
			<table class="min-w-full border-collapse text-left text-sm">
				<thead class="bg-black">
					<tr class="border-b border-gray-800 text-[11px] tracking-[0.16em] text-gray-500 uppercase">
						<th class="px-4 py-3 font-medium">Time</th>
						<th class="px-4 py-3 font-medium">Object</th>
						<th class="px-4 py-3 font-medium">Type</th>
						<th class="px-4 py-3 font-medium">Confidence</th>
						<th class="px-4 py-3 font-medium">Camera</th>
						<th class="px-4 py-3 font-medium">Position</th>
					</tr>
				</thead>
				<tbody class="font-mono text-xs text-gray-200">
					{#if isLoading}
						<tr>
							<td colspan="6" class="px-4 py-8 text-center text-sm text-gray-500">
								Loading...
							</td>
						</tr>
					{:else if items.length === 0}
						<tr>
							<td colspan="6" class="px-4 py-8 text-center text-sm text-gray-500">
								No detections returned for this query yet.
							</td>
						</tr>
					{:else}
						{#each items as item (`${item.ts}|${item.camera}|${item.object_id}`)}
							<tr
								class={`border-b border-gray-900/80 transition hover:bg-white/[0.03] ${
									highlightObjectId != null && item.object_id === highlightObjectId
										? 'bg-amber-400/10'
										: ''
								}`}
							>
								<td class="px-4 py-3 align-top" title={item.ts}>{displayTime(item.ts)}</td>
								<td class="px-4 py-3 align-top">{item.object_id}</td>
								<td class="px-4 py-3 align-top">{item.object_type}</td>
								<td class="px-4 py-3 align-top">{item.confidence.toFixed(2)}</td>
								<td class="px-4 py-3 align-top">{item.camera}</td>
								<td class="px-4 py-3 align-top text-gray-400">{displayPosition(item)}</td>
							</tr>
						{/each}
					{/if}
				</tbody>
			</table>
		</div>
	</div>
</section>
