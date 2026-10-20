<script lang="ts">
	// What every connected vehicle sends on V2X and what it hears inside the reception
	// radius: the view an onboard unit has. Docked in the left column; the rows are
	// replaced in place from the server's snapshots, never appended.
	import { onDestroy } from 'svelte';
	import { driveConnected } from '$lib/stores/driveSocket';
	import {
		MAX_RADIUS_M,
		MIN_RADIUS_M,
		formatAge,
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
	import { formatHeading, formatPosition, formatSpeed, lightsLabel } from '$lib/v2xFormat';
	import type { V2xPerspectiveVehicle } from '$lib/types';

	let vehicles = $derived($v2xPerspective?.vehicles ?? []);

	// subscribe whenever the socket is (re)connected; the server stops pushing when it drops
	$effect(() => {
		if ($driveConnected) subscribeV2xPerspective();
	});

	onDestroy(() => {
		unsubscribeV2xPerspective();
	});

	function applyRadius(e: Event) {
		const value = Number((e.target as HTMLInputElement).value);
		if (Number.isFinite(value)) setV2xRadius(value);
	}

	function kindLabel(v: V2xPerspectiveVehicle): string {
		if (v.subkind === 'ego') return 'you';
		if (v.kind === 'ghost') return v.org ? `${v.org} ghost` : 'ghost';
		if (v.kind === 'voices') return 'ours';
		return v.kind;
	}
</script>

<section class="font-mono text-[11px] leading-[16px] text-gray-100" data-testid="v2x-perspective">
	<div class="flex items-center justify-between gap-2 pb-1">
		<span class="text-[10px] uppercase tracking-widest text-gray-500">V2X · {vehicles.length} connected</span>
		<label class="flex items-center gap-1 text-[10px] text-gray-400">
			hears within
			<input
				type="number"
				min={MIN_RADIUS_M}
				max={MAX_RADIUS_M}
				step="10"
				value={$v2xRadiusM}
				onchange={applyRadius}
				aria-label="Reception radius in metres"
				class="w-[6ch] rounded border border-gray-700 bg-gray-900/80 px-1 text-right text-[10px] text-gray-100 focus:border-cyan-500 focus:outline-none"
				data-testid="v2x-radius"
			/>
			m
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
				<span class="shrink-0 text-[10px] text-gray-500">{kindLabel(v)}</span>
				{#if v.emergency}<span class="shrink-0 text-[10px] text-amber-300">ERV</span>{/if}
				<span class="ml-auto shrink-0 text-gray-400" data-testid="v2x-vehicle-summary">
					{v.tx ? `${Number(v.tx.rate_hz ?? 0).toFixed(0)} Hz` : 'silent'} · hears {v.rx_count}
				</span>
			</button>

			{#if open}
				<div class="pb-2 pl-3" data-testid="v2x-vehicle-details">
					<div class="text-gray-500">sends</div>
					{#if v.tx}
						<div class="truncate">{txLine(v.tx)}</div>
						{#if v.tx.summary}
							<div class="truncate text-gray-300">
								id {v.tx.summary.id ?? '?'} · msg {v.tx.summary.msg_cnt ?? '?'} · {formatPosition(v.tx.summary)}
							</div>
							{#if lightsLabel(v.tx.summary) && lightsLabel(v.tx.summary) !== 'off'}
								<div class="text-amber-300">{lightsLabel(v.tx.summary)}{v.tx.summary.brakes ? ' · BRAKE' : ''}</div>
							{/if}
						{/if}
						<div class="text-gray-500">{formatAge(v.tx.age_s)} · {v.tx.source} · {v.tx.size} B</div>
					{:else}
						<div class="text-gray-500">no BSM from this vehicle</div>
					{/if}

					<div class="pt-1 text-gray-500">hears · {v.rx_count} within {$v2xRadiusM} m</div>
					{#each v.rx as r (r.key)}
						<div class="flex items-baseline gap-2" data-testid="v2x-rx-row" title={`${r.name}: ${formatDistance(r.distance_m)} ${relBearingLabel(r.rel_bearing_deg)}, ${formatSpeed(r.summary?.speed_mps)}, heading ${formatHeading(r.summary?.heading_deg)}, ${formatAge(r.age_s)}`}>
							<span class="w-[6ch] shrink-0 text-right" data-testid="v2x-rx-distance">{formatDistance(r.distance_m)}</span>
							<span class="w-[6ch] shrink-0 text-gray-400">{relBearingLabel(r.rel_bearing_deg)}</span>
							<span class="truncate">{r.name}{r.emergency ? ' ERV' : ''}</span>
							<span class="ml-auto shrink-0 text-gray-400">{formatSpeed(r.summary?.speed_mps)}</span>
						</div>
					{:else}
						<div class="text-gray-500">nothing in range</div>
					{/each}
				</div>
			{/if}
		</div>
	{:else}
		<div class="border-t border-white/10 pt-1 text-gray-500">
			{$v2xPerspective ? 'no connected vehicles' : 'waiting for the V2X feed'}
		</div>
	{/each}
</section>
