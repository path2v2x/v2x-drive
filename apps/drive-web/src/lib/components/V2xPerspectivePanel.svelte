<script lang="ts">
	// What every connected vehicle sends on V2X and what it hears inside the reception
	// range: the view an onboard unit has. Docked in the left column; the rows are
	// replaced in place from the server's snapshots, never appended.
	import { onDestroy } from 'svelte';
	import { driveConnected } from '$lib/stores/driveSocket';
	import {
		RADIUS_PRESETS,
		formatDistance,
		relBearingLabel,
		setV2xRadius,
		subscribeV2xPerspective,
		toggleV2xExpanded,
		txLine,
		unsubscribeV2xPerspective,
		v2xExpanded,
		v2xPerspective,
		v2xRadiusM
	} from '$lib/stores/v2xPerspective';
	import { formatSpeed } from '$lib/v2xFormat';
	import type { V2xPerspectiveVehicle } from '$lib/types';

	let vehicles = $derived($v2xPerspective?.vehicles ?? []);
	// the stored radius may be a value outside the presets (set before this list existed)
	let radiusOptions = $derived(
		RADIUS_PRESETS.includes($v2xRadiusM) ? RADIUS_PRESETS : [...RADIUS_PRESETS, $v2xRadiusM].sort((a, b) => a - b)
	);

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

	/** Only the labels that add information: your own car, and the organization of a ghost. */
	function tag(v: V2xPerspectiveVehicle): string {
		if (v.subkind === 'ego') return 'you';
		if (v.kind === 'ghost') return v.org || 'ghost';
		return '';
	}
</script>

<section class="font-mono text-[11px] leading-[16px] text-gray-100" data-testid="v2x-perspective">
	<div class="flex items-center justify-between gap-2 pb-1">
		<span class="text-[10px] uppercase tracking-widest text-gray-500">V2X · {vehicles.length} vehicles</span>
		<label class="flex items-center gap-1 text-[10px] text-gray-400">
			range
			<select
				value={$v2xRadiusM}
				onchange={applyRadius}
				aria-label="Reception range in metres"
				class="cursor-pointer rounded border border-gray-700 bg-gray-900/80 px-1 text-[10px] text-gray-100 focus:border-cyan-500 focus:outline-none"
				data-testid="v2x-radius"
			>
				{#each radiusOptions as r (r)}
					<option value={r}>{r} m</option>
				{/each}
			</select>
		</label>
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
				<div class="grid grid-cols-[5ch_1fr] gap-x-2 pb-2 pl-3" data-testid="v2x-vehicle-details">
					<span class="text-gray-500">sends</span>
					<span class="truncate" data-testid="v2x-tx-line">{txLine(v.tx)}</span>

					<span class="text-gray-500">hears</span>
					<div class="min-w-0">
						{#each v.rx as r (r.key)}
							<div class="grid grid-cols-[1fr_5ch_6ch_8ch] gap-x-1" data-testid="v2x-rx-row">
								<span class="truncate">{r.name}{r.emergency ? ' ERV' : ''}</span>
								<span class="text-right" data-testid="v2x-rx-distance">{formatDistance(r.distance_m)}</span>
								<span class="text-gray-400">{relBearingLabel(r.rel_bearing_deg)}</span>
								<span class="text-right text-gray-400">{formatSpeed(r.summary?.speed_mps, 0)}</span>
							</div>
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
