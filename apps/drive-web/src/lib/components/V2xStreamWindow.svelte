<script lang="ts">
	import FloatingWindow from './FloatingWindow.svelte';
	import {
		RING_MAX,
		clearV2xStream,
		exportV2xStream,
		pauseV2xStream,
		requestV2xDecode,
		setV2xDetail,
		v2xDecoded,
		v2xDetail,
		v2xServerDropped,
		v2xStreams,
		v2xVehicles
	} from '$lib/stores/v2xViewer';
	import {
		formatHeading,
		formatPosition,
		formatRxTime,
		formatSpeed,
		isBsmSummary,
		lightsLabel,
		rowLine,
		shortTypeId
	} from '$lib/v2xFormat';
	import type { V2xMessageRow } from '$lib/types';

	interface Props {
		windowId: string;
		key: string;
		onClose: () => void;
	}

	let { windowId, key, onClose }: Props = $props();

	let stream = $derived($v2xStreams.get(key));
	let vehicle = $derived($v2xVehicles.find((v) => v.key === key));
	let showRaw = $state(false);
	let typeFilter = $state<'all' | 'BSM' | 'other'>('all');
	let expandedSeq = $state<number | null>(null);

	let rows = $derived(
		(stream?.rows ?? []).filter((r) =>
			typeFilter === 'all' ? true : typeFilter === 'BSM' ? r.msg_type === 'BSM' : r.msg_type !== 'BSM'
		)
	);
	let last = $derived(stream?.last ?? null);
	let lastSummary = $derived(last && last.msg_type === 'BSM' && isBsmSummary(last.summary) ? last.summary : null);
	let lastLights = $derived(lastSummary ? lightsLabel(lastSummary) : null);
	let subtitle = $derived(
		vehicle
			? `${vehicle.org || '?'} · ${shortTypeId(vehicle.type_id) || vehicle.kind}${vehicle.emergency ? ' · emergency vehicle' : ''}`
			: 'message stream'
	);

	function toggleExpand(row: V2xMessageRow): void {
		if (expandedSeq === row.seq) {
			expandedSeq = null;
			return;
		}
		expandedSeq = row.seq;
		if (row.decoded === undefined && !row.error) requestV2xDecode(row.raw_hex);
	}

	function detailFor(row: V2xMessageRow): unknown {
		if (row.decoded !== undefined) return row.decoded;
		const cached = $v2xDecoded.get(row.raw_hex);
		if (cached) return cached.error ? { error: cached.error, raw_hex: row.raw_hex } : cached.decoded;
		return row.summary ?? { raw_hex: row.raw_hex };
	}

	function download(): void {
		const text = exportV2xStream(key);
		if (!text) return;
		const blob = new Blob([text], { type: 'application/x-ndjson' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `v2x-${key.replace(/[^A-Za-z0-9_-]+/g, '_')}.jsonl`;
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 5000);
	}

	// Stick-to-bottom auto scroll (same policy as the packet log).
	let listEl = $state<HTMLDivElement | null>(null);
	let stick = true;
	$effect(() => {
		void rows;
		if (listEl && stick) listEl.scrollTop = listEl.scrollHeight;
	});
	function onScroll(): void {
		if (!listEl) return;
		stick = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 40;
	}
</script>

<FloatingWindow
	id={windowId}
	title={key}
	{subtitle}
	accent={vehicle?.emergency ? 'text-red-300' : 'text-emerald-300'}
	{onClose}
	testid="v2x-stream-window"
>
	{#snippet toolbar()}
		<span class="font-mono text-[10px] text-gray-400" title="messages per second (client side)">{stream?.rateHz ?? 0} Hz</span>
	{/snippet}

	<div
		class="grid grid-cols-4 gap-x-3 border-b border-gray-800 px-3 py-1.5 font-mono text-[11px] text-gray-200"
		data-testid="v2x-stream-summary"
	>
		{#if lastSummary}
			<div class="truncate"><span class="text-gray-500">id </span>{lastSummary.id ?? '–'}</div>
			<div class="truncate"><span class="text-gray-500">speed </span>{formatSpeed(lastSummary.speed_mps)}</div>
			<div class="truncate"><span class="text-gray-500">hdg </span>{formatHeading(lastSummary.heading_deg)}</div>
			<div class="truncate {lastLights && lastLights !== 'off' ? 'text-red-300' : ''}">
				<span class="text-gray-500">lights </span>{lastLights ?? 'n/a'}
			</div>
			<div class="col-span-4 truncate text-gray-400">
				{formatPosition(lastSummary)}{last?.source === 'udp' ? ' · udp tap' : ''}
			</div>
		{:else}
			<div class="col-span-4 italic text-gray-500">
				{last ? `${last.msg_type} · ${last.size} B${last.error ? ` · ${last.error}` : ''}` : 'waiting for messages…'}
			</div>
		{/if}
	</div>

	<div class="flex flex-wrap items-center gap-1.5 border-b border-gray-800 px-2 py-1 text-[11px]">
		<button
			type="button"
			onclick={() => pauseV2xStream(key)}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600"
		>{stream?.paused ? 'Resume' : 'Pause'}</button>
		<button
			type="button"
			onclick={() => clearV2xStream(key)}
			disabled={(stream?.rows.length ?? 0) === 0}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-40"
		>Clear</button>
		<button
			type="button"
			onclick={() => (showRaw = !showRaw)}
			aria-pressed={showRaw}
			class="cursor-pointer rounded px-2 py-0.5 text-white {showRaw ? 'bg-cyan-600' : 'bg-gray-700 hover:bg-gray-600'}"
			title="Show the UPER frame as hex under every row"
		>Raw</button>
		<button
			type="button"
			onclick={() => setV2xDetail($v2xDetail === 'full' ? 'compact' : 'full')}
			aria-pressed={$v2xDetail === 'full'}
			class="cursor-pointer rounded px-2 py-0.5 text-white {$v2xDetail === 'full' ? 'bg-cyan-600' : 'bg-gray-700 hover:bg-gray-600'}"
			title="Full: rows carry the decoded J2735 body (all windows)"
		>Full</button>
		<select
			bind:value={typeFilter}
			aria-label="Message type filter"
			class="cursor-pointer rounded border border-gray-600 bg-gray-800 px-1 py-0.5 text-[11px] text-gray-200"
		>
			<option value="all">all types</option>
			<option value="BSM">BSM only</option>
			<option value="other">non-BSM</option>
		</select>
		<span class="flex-1"></span>
		<button
			type="button"
			onclick={download}
			disabled={(stream?.rows.length ?? 0) === 0}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-40"
			title="Download the buffered rows as JSON Lines"
		>.jsonl</button>
	</div>

	<div
		bind:this={listEl}
		onscroll={onScroll}
		class="flex-1 overflow-auto px-1 py-1 font-mono text-[11px] leading-relaxed"
		data-testid="v2x-stream-rows"
	>
		{#if rows.length === 0}
			<p class="p-2 italic text-gray-600">No messages yet.</p>
		{:else}
			{#each rows as row (row.seq)}
				<button
					type="button"
					onclick={() => toggleExpand(row)}
					aria-expanded={expandedSeq === row.seq}
					class="block w-full cursor-pointer rounded px-1 text-left hover:bg-white/5"
					data-testid="v2x-row"
				>
					<div class="grid grid-cols-[86px_40px_1fr] gap-2">
						<span class="text-[10px] text-gray-600 tabular-nums">{formatRxTime(row.t_rx)}</span>
						<span class={row.msg_type === 'BSM' ? 'text-emerald-400' : 'text-amber-300'}>{row.msg_type}</span>
						<span class="truncate {row.error ? 'text-red-300' : 'text-gray-200'}">
							{row.error ? `decode error: ${row.error}` : rowLine(row)}
						</span>
					</div>
					{#if showRaw}
						<div class="break-all pl-[94px] text-[10px] text-gray-500" data-testid="v2x-row-raw">{row.raw_hex}</div>
					{/if}
				</button>
				{#if expandedSeq === row.seq}
					<pre
						class="mx-1 mb-1 max-h-64 overflow-auto rounded bg-black/40 p-2 text-[10px] text-gray-300"
						data-testid="v2x-row-detail"
					>{JSON.stringify(detailFor(row), null, 2)}</pre>
				{/if}
			{/each}
		{/if}
	</div>

	<div class="flex items-center gap-3 border-t border-gray-800 px-3 py-1 text-[10px] text-gray-500">
		<span>{rows.length} shown · {stream?.total ?? 0} received · last {RING_MAX} kept</span>
		{#if stream?.paused}
			<span class="text-amber-300">paused</span>
		{/if}
		{#if $v2xServerDropped > 0}
			<span class="text-amber-300">{$v2xServerDropped} dropped by server (all streams)</span>
		{/if}
	</div>
</FloatingWindow>
