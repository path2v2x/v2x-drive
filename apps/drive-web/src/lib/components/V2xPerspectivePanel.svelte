<script lang="ts">
	// What every connected vehicle sends on V2X and what it hears inside the reception
	// range: the view an onboard unit has. Docked in a sidebar; the rows are replaced in
	// place from the server's snapshots, never appended. "simple" shows speed, heading and
	// distances; "full" shows every decoded BSM field and the raw J2735 body.
	import { onDestroy } from 'svelte';
	import { driveConnected } from '$lib/stores/driveSocket';
	import {
		RADIUS_PRESETS,
		formatDistance,
		jsonPretty,
		relBearingLabel,
		setV2xDetail,
		setV2xRadius,
		subscribeV2xPerspective,
		summaryFields,
		toggleV2xExpanded,
		txLine,
		unsubscribeV2xPerspective,
		v2xDetail,
		v2xExpanded,
		v2xPerspective,
		v2xRadiusM,
		type V2xFieldSource
	} from '$lib/stores/v2xPerspective';
	import { formatSpeed } from '$lib/v2xFormat';
	import type { V2xPerspectiveRx, V2xPerspectiveVehicle } from '$lib/types';

	let vehicles = $derived($v2xPerspective?.vehicles ?? []);
	let full = $derived($v2xDetail === 'full');
	// the stored radius may be a value outside the presets (set before this list existed)
	let radiusOptions = $derived(
		RADIUS_PRESETS.includes($v2xRadiusM) ? RADIUS_PRESETS : [...RADIUS_PRESETS, $v2xRadiusM].sort((a, b) => a - b)
	);
	// heard senders whose full fields are open, as "vehicle|sender"
	let rxOpen = $state(new Set<string>());

	// subscribe whenever the socket is (re)connected; the server stops pushing when it drops
	$effect(() => {
		if ($driveConnected) subscribeV2xPerspective();
	});

	onDestroy(() => {
		unsubscribeV2xPerspective();
	});

	function applyRadius(e: Event) {
		const value = Number((e.target as HTMLSelectElement).value);
		if (Number.isFinite(value)) setV2xRadius(value);
	}

	function toggleRx(vehicle: string, sender: string) {
		const id = `${vehicle}|${sender}`;
		const next = new Set(rxOpen);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		rxOpen = next;
	}

	/** Only the labels that add information: your own car, and the organization of a ghost. */
	function tag(v: V2xPerspectiveVehicle): string {
		if (v.subkind === 'ego') return 'you';
		if (v.kind === 'ghost') return v.org || 'ghost';
		return '';
	}
</script>

{#snippet heardRow(r: V2xPerspectiveRx)}
	<span class="truncate">{r.name}{r.emergency ? ' ERV' : ''}</span>
	<span class="text-right" data-testid="v2x-rx-distance">{formatDistance(r.distance_m)}</span>
	<span class="text-gray-400">{relBearingLabel(r.rel_bearing_deg)}</span>
	<span class="text-right text-gray-400">{formatSpeed(r.summary?.speed_mps, 0)}</span>
{/snippet}

{#snippet fields(entry: V2xFieldSource)}
	<div class="mt-1 grid grid-cols-[11ch_1fr] gap-x-2 text-[10px] leading-[14px]" data-testid="v2x-fields">
		{#each summaryFields(entry) as [label, value] (label)}
			<span class="text-gray-500">{label}</span>
			<span class="truncate text-gray-300" title={value}>{value}</span>
		{/each}
	</div>
	{#if entry.decoded}
		<pre class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-black/40 p-1 text-[10px] leading-[13px] text-gray-400" data-testid="v2x-raw">{jsonPretty(entry.decoded)}</pre>
	{/if}
{/snippet}

<section class="font-mono text-[12px] leading-[18px] text-gray-100" data-testid="v2x-perspective">
	<!-- the controls move to a second line, right-aligned, when the column is too narrow for one -->
	<div class="flex flex-wrap items-center gap-x-2 gap-y-1 pb-1">
		<span class="shrink-0 whitespace-nowrap text-[10px] uppercase tracking-widest text-gray-500">V2X · {vehicles.length} vehicles</span>
		<div class="ml-auto flex items-center gap-1 text-[10px] text-gray-400">
			<button
				type="button"
				onclick={() => setV2xDetail(full ? 'simple' : 'full')}
				aria-pressed={full}
				title={full ? 'Show the simple view' : 'Show every BSM field and the raw message'}
				class="cursor-pointer rounded border border-gray-700 px-1.5 text-[10px] {full ? 'bg-white/15 text-white' : 'bg-gray-900/80 text-gray-300 hover:text-white'}"
				data-testid="v2x-detail"
			>
				{full ? 'full' : 'simple'}
			</button>
			<select
				value={$v2xRadiusM}
				onchange={applyRadius}
				aria-label="Reception range in metres"
				title="How far a vehicle hears the others"
				class="cursor-pointer rounded border border-gray-700 bg-gray-900/80 px-1 text-[10px] text-gray-100 focus:border-cyan-500 focus:outline-none"
				data-testid="v2x-radius"
			>
				{#each radiusOptions as r (r)}
					<option value={r}>range {r} m</option>
				{/each}
			</select>
		</div>
	</div>

	{#each vehicles as v (v.key)}
		{@const open = $v2xExpanded.has(v.key)}
		<div class="border-t border-white/10" data-testid="v2x-vehicle">
			<button
				type="button"
				onclick={() => toggleV2xExpanded(v.key)}
				aria-expanded={open}
				class="flex w-full cursor-pointer items-center gap-1 py-[3px] text-left hover:bg-white/5"
				data-testid="v2x-vehicle-toggle"
			>
				<span class="w-[1ch] shrink-0 text-gray-500">{open ? '▾' : '▸'}</span>
				<span class="truncate font-semibold">{v.name}</span>
				{#if tag(v)}<span class="shrink-0 text-[10px] text-gray-500">{tag(v)}</span>{/if}
				{#if v.emergency}<span class="shrink-0 text-[10px] text-amber-300">ERV</span>{/if}
				<span class="ml-auto shrink-0 text-gray-400" data-testid="v2x-vehicle-summary">
					{v.tx ? `${Number(v.tx.rate_hz ?? 0).toFixed(0)} Hz` : 'silent'} · hears {v.rx_count}
				</span>
			</button>

			{#if open}
				<div class="grid grid-cols-[5ch_1fr] gap-x-2 pb-2 pl-1" data-testid="v2x-vehicle-details">
					<span class="text-gray-500">sends</span>
					<div class="min-w-0">
						<div class="truncate" data-testid="v2x-tx-line">{txLine(v.tx)}</div>
						{#if full && v.tx}
							{@render fields(v.tx)}
						{/if}
					</div>

					<span class="text-gray-500">hears</span>
					<div class="min-w-0">
						{#each v.rx as r (r.key)}
							{#if full}
								<button
									type="button"
									onclick={() => toggleRx(v.key, r.key)}
									aria-expanded={rxOpen.has(`${v.key}|${r.key}`)}
									class="grid w-full cursor-pointer grid-cols-[1fr_5ch_6ch_8ch] gap-x-1 text-left hover:bg-white/5"
									data-testid="v2x-rx-row"
								>
									{@render heardRow(r)}
								</button>
								{#if rxOpen.has(`${v.key}|${r.key}`)}
									<div class="mb-1 pl-2">{@render fields(r)}</div>
								{/if}
							{:else}
								<div class="grid grid-cols-[1fr_5ch_6ch_8ch] gap-x-1" data-testid="v2x-rx-row">
									{@render heardRow(r)}
								</div>
							{/if}
						{:else}
							<div class="text-gray-500">nobody in range</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{:else}
		<div class="border-t border-white/10 pt-1 text-gray-500">
			{$v2xPerspective ? 'no vehicles on V2X' : 'waiting for the V2X feed'}
		</div>
	{/each}
</section>
