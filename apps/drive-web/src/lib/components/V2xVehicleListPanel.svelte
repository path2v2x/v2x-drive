<script lang="ts">
	import {
		closeV2xStream,
		groupV2xVehicles,
		openV2xStream,
		openV2xStreams,
		requestV2xVehicleList,
		setV2xDetail,
		v2xDetail,
		v2xFeedStatus,
		v2xServerDropped,
		v2xStreams,
		v2xVehicles
	} from '$lib/stores/v2xViewer';
	import { formatSpeed, shortTypeId } from '$lib/v2xFormat';
	import type { V2xVehicleEntry } from '$lib/types';

	let groups = $derived(groupV2xVehicles($v2xVehicles));
	let openKeys = $derived(new Set($v2xStreams.keys()));
	let feed = $derived($v2xFeedStatus);
	let streamOk = $derived(Boolean(feed?.json_stream?.connected));
	let decoderOk = $derived(Boolean(feed?.decoder?.available));
	let streamRate = $derived(Number(feed?.json_stream?.records_per_s ?? 0));
	let udpRate = $derived(Number(feed?.udp?.datagrams_per_s ?? 0));

	function feedDot(v: V2xVehicleEntry): string {
		switch (v.feed?.source) {
			case 'tena':
				return 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.7)]';
			case 'udp':
				return 'bg-amber-400';
			case 'stale':
				return 'bg-gray-500';
			default:
				return 'bg-gray-700';
		}
	}

	function feedText(v: V2xVehicleEntry): string {
		const f = v.feed;
		if (!f) return 'no feed';
		const rate = Number(f.rate_hz ?? 0).toFixed(0);
		switch (f.source) {
			case 'tena':
				return `${rate} Hz`;
			case 'udp':
				return `${rate} Hz udp`;
			case 'stale':
				return typeof f.age_s === 'number' ? `stale ${Math.round(f.age_s)}s` : 'stale';
			default:
				return 'no feed';
		}
	}

	function toggle(v: V2xVehicleEntry, checked: boolean): void {
		if (checked) openV2xStream(v.key);
		else closeV2xStream(v.key);
	}

	function toggleDetail(): void {
		setV2xDetail($v2xDetail === 'full' ? 'compact' : 'full');
	}
</script>

<div
	class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 px-3 py-1.5 text-[11px] text-gray-400"
	data-testid="v2x-feed-status"
>
	<span class="flex items-center gap-1" title="DT JSON streamer (TENA subscriber) connected to this server">
		<span class="h-2 w-2 rounded-full {streamOk ? 'bg-emerald-400' : 'bg-red-500'}"></span>
		stream {streamOk ? `${streamRate.toFixed(0)}/s` : 'off'}
	</span>
	<span title="raw UPER datagrams from the TENA V2X adapter">udp {udpRate.toFixed(0)}/s</span>
	<span class={decoderOk ? '' : 'text-amber-300'} title={feed?.decoder?.error ?? 'J2735 decoder'}>
		decoder {decoderOk ? 'ok' : 'missing'}
	</span>
	{#if feed?.roster?.scenario}
		<span class="truncate text-gray-500" title="VOICES scenario">{feed.roster.scenario}</span>
	{/if}
	<span class="flex-1"></span>
	<button
		type="button"
		onclick={() => requestV2xVehicleList()}
		class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600"
		title="Ask the server for the vehicle list again"
	>Refresh</button>
	<button
		type="button"
		onclick={toggleDetail}
		aria-pressed={$v2xDetail === 'full'}
		class="cursor-pointer rounded px-2 py-0.5 {$v2xDetail === 'full' ? 'bg-cyan-600 text-white' : 'bg-gray-700 text-white hover:bg-gray-600'}"
		title="Full: every row carries the decoded J2735 body. Compact: summary only (decode on demand)."
	>{$v2xDetail === 'full' ? 'Full' : 'Compact'}</button>
</div>

<div class="flex-1 overflow-auto px-2 py-1 text-xs">
	{#if $v2xVehicles.length === 0}
		<p class="p-3 italic leading-relaxed text-gray-500" data-testid="v2x-empty">
			No vehicles with a V2X feed.
			{#if feed === null}
				Waiting for the server…
			{:else if !streamOk}
				The DT JSON streamer is not connected: start the DT session (and the background
				traffic demo) on this host, then refresh.
			{:else}
				The DT scenario has no vehicles publishing messages yet.
			{/if}
		</p>
	{:else}
		{#each groups as g (g.id)}
			<div class="mt-1 flex items-center justify-between px-1 text-[10px] uppercase tracking-wider text-gray-500">
				<span>{g.label} · {g.vehicles.length}</span>
				{#if g.vehicles.length > 1}
					<button
						type="button"
						onclick={() => openV2xStreams(g.vehicles.map((v) => v.key))}
						class="cursor-pointer normal-case tracking-normal text-cyan-400 hover:text-cyan-200"
					>open all</button>
				{/if}
			</div>
			{#each g.vehicles as v (v.key)}
				<label
					class="flex cursor-pointer items-center gap-2 rounded px-1 pt-1 hover:bg-white/5"
					data-testid="v2x-vehicle-row"
				>
					<input
						type="checkbox"
						checked={openKeys.has(v.key)}
						onchange={(e) => toggle(v, e.currentTarget.checked)}
						class="cursor-pointer accent-cyan-500"
						aria-label="Show messages from {v.key}"
					/>
					<span class="h-2 w-2 shrink-0 rounded-full {feedDot(v)}" title={v.feed?.source ?? 'none'}></span>
					<span class="min-w-0 flex-1 truncate font-mono text-gray-100">{v.key}</span>
					{#if v.emergency}
						<span
							class="rounded bg-red-600/70 px-1 text-[9px] font-bold text-white"
							title={v.special_lights ? 'emergency vehicle, lights on' : 'emergency vehicle'}
						>{v.special_lights ? 'LIGHTS' : 'ERV'}</span>
					{/if}
					{#if v.org}
						<span class="rounded bg-gray-700 px-1 text-[9px] text-gray-300">{v.org}</span>
					{/if}
					<span class="w-16 shrink-0 text-right font-mono text-[10px] text-gray-400">{feedText(v)}</span>
				</label>
				<div class="truncate pb-1 pl-9 text-[10px] text-gray-500">
					{shortTypeId(v.type_id) || v.subkind || 'vehicle'}
					{#if typeof v.speed_mps === 'number'} · {formatSpeed(v.speed_mps)}{/if}
					{#if !v.in_world} · not in this world{/if}
				</div>
			{/each}
		{/each}
	{/if}
</div>

<div class="flex items-center gap-3 border-t border-gray-800 px-3 py-1 text-[10px] text-gray-500">
	<span>{$v2xVehicles.length} listed · tick a vehicle to open its message window</span>
	{#if $v2xServerDropped > 0}
		<span class="text-amber-300">{$v2xServerDropped} dropped by server</span>
	{/if}
</div>
