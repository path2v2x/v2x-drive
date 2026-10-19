<script lang="ts">
	import { onMount } from 'svelte';
	import { driveConnected, sessionState } from '$lib/stores/driveSocket';
	import {
		clearFleetEvents,
		despawnFleet,
		despawnFleetVehicle,
		fleetEvents,
		fleetStats,
		fleetTracks,
		fleetVehicles,
		requestFleet,
		setErvState,
		spawnCiviliansAhead,
		spawnErv,
		stateTone
	} from '$lib/stores/v2xFleet';
	import type { V2xFleetVehicle } from '$lib/types';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();

	let ervProfile = $state<'ambulance' | 'fire' | 'police'>('ambulance');
	let ervPlacement = $state<'behind' | 'random'>('behind');
	let ervDistance = $state(150);
	let civilianCount = $state(2);
	let civilianAhead = $state(60);
	let note = $state('');

	let sessionActive = $derived($driveConnected && $sessionState === 'driving');
	let vehicles = $derived($fleetVehicles);
	let ervs = $derived(vehicles.filter((v) => v.kind === 'erv'));
	let civilians = $derived(vehicles.filter((v) => v.kind === 'civilian'));
	let groups = $derived<{ label: string; items: V2xFleetVehicle[] }[]>([
		{ label: 'Emergency vehicles', items: ervs },
		{ label: 'Civilians', items: civilians }
	]);
	let recentEvents = $derived($fleetEvents.slice(-12).reverse());
	let full = $derived(($fleetStats?.count ?? vehicles.length) >= ($fleetStats?.max ?? 12));

	onMount(() => {
		requestFleet();
	});

	function toneClass(state: string): string {
		switch (stateTone(state)) {
			case 'calm':
				return 'bg-emerald-700/60 text-emerald-100';
			case 'alert':
				return 'bg-amber-600/60 text-amber-50';
			case 'yield':
				return 'bg-cyan-700/60 text-cyan-50';
			case 'stopped':
				return 'bg-red-700/60 text-red-50';
			case 'done':
				return 'bg-gray-600/70 text-gray-100';
			default:
				return 'bg-gray-700 text-gray-200';
		}
	}

	function kmh(v: number | null | undefined): string {
		return typeof v === 'number' ? `${(v * 3.6).toFixed(0)} km/h` : '–';
	}

	function doSpawnErv() {
		if (ervPlacement === 'behind' && !sessionActive) {
			note = 'Start a drive session to spawn behind the ego, or choose a random spot.';
			return;
		}
		note = spawnErv(ervProfile, ervPlacement, ervDistance) ? `Spawning ${ervProfile}…` : 'Not connected.';
	}

	function doSpawnCivilians() {
		if (!sessionActive) {
			note = 'Start a drive session first: civilians spawn ahead of your car.';
			return;
		}
		const sent = spawnCiviliansAhead(civilianCount, civilianAhead);
		note = sent > 0 ? `Spawning ${sent} civilian${sent === 1 ? '' : 's'} ahead…` : 'Not connected.';
	}

	function toggle(v: V2xFleetVehicle, key: 'siren' | 'lights') {
		setErvState(v.actor_id, { [key]: !(v[key] as boolean) });
	}

	function fmtTime(t: number): string {
		const d = new Date(t * 1000);
		return d.toLocaleTimeString([], { hour12: false });
	}
</script>

<div
	class="absolute bottom-16 right-2 z-30 flex max-h-[34rem] w-[22rem] flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-900/95 text-gray-200 shadow-xl backdrop-blur-md pointer-events-auto"
	data-testid="v2x-fleet-panel"
>
	<div class="flex items-center justify-between border-b border-gray-700 px-3 py-2">
		<span class="text-sm font-semibold tracking-wide text-rose-300">V2X fleet</span>
		<div class="flex items-center gap-1 text-[11px]">
			<button
				type="button"
				onclick={() => requestFleet()}
				class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600"
			>Refresh</button>
			<button
				type="button"
				onclick={() => despawnFleet()}
				disabled={vehicles.length === 0}
				class="cursor-pointer rounded bg-red-700/80 px-2 py-0.5 text-white hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-40"
				title="Destroy every fleet vehicle"
			>Despawn all</button>
			<button
				type="button"
				onclick={onClose}
				class="ml-1 cursor-pointer px-1 text-xl leading-none text-gray-400 hover:text-white"
				aria-label="Close fleet panel"
			>×</button>
		</div>
	</div>

	<div class="flex flex-col gap-2 border-b border-gray-800 p-3 text-xs">
		<div class="flex items-center gap-2">
			<select bind:value={ervProfile} aria-label="Emergency vehicle profile"
				class="rounded border border-gray-600 bg-gray-800 px-1 py-1 text-gray-100">
				<option value="ambulance">Ambulance</option>
				<option value="fire">Fire truck</option>
				<option value="police">Police</option>
			</select>
			<select bind:value={ervPlacement} aria-label="Emergency vehicle placement"
				class="rounded border border-gray-600 bg-gray-800 px-1 py-1 text-gray-100">
				<option value="behind">behind the ego</option>
				<option value="random">random spot</option>
			</select>
			{#if ervPlacement === 'behind'}
				<input type="number" bind:value={ervDistance} min="30" max="400" step="10" aria-label="ERV distance behind the ego"
					class="w-16 rounded border border-gray-600 bg-gray-800 px-1 py-1 font-mono text-gray-100" />
				<span class="text-gray-500">m</span>
			{/if}
			<button
				type="button"
				onclick={doSpawnErv}
				disabled={full}
				class="ml-auto cursor-pointer rounded bg-rose-600 px-2 py-1 font-medium text-white hover:bg-rose-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
				data-testid="spawn-erv"
			>Spawn ERV</button>
		</div>
		<div class="flex items-center gap-2">
			<input type="number" bind:value={civilianCount} min="1" max="8" aria-label="Number of civilians"
				class="w-12 rounded border border-gray-600 bg-gray-800 px-1 py-1 font-mono text-gray-100" />
			<span class="text-gray-400">civilians,</span>
			<input type="number" bind:value={civilianAhead} min="20" max="300" step="10" aria-label="Civilian distance ahead of the ego"
				class="w-16 rounded border border-gray-600 bg-gray-800 px-1 py-1 font-mono text-gray-100" />
			<span class="text-gray-400">m ahead of the ego</span>
			<button
				type="button"
				onclick={doSpawnCivilians}
				disabled={full}
				class="ml-auto cursor-pointer rounded bg-sky-600 px-2 py-1 font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
				data-testid="spawn-civilians"
			>Spawn</button>
		</div>
		{#if note}
			<p class="text-[11px] text-amber-300" role="status">{note}</p>
		{/if}
	</div>

	<div class="min-h-0 flex-1 overflow-auto px-2 py-1 text-xs">
		{#if vehicles.length === 0}
			<p class="p-2 italic text-gray-500" data-testid="fleet-empty">
				No fleet vehicles. Spawn an emergency vehicle and a few civilians: the civilians pull
				over for an ERV running lights and siren and slow down past a stopped one.
			</p>
		{/if}
		{#each groups as g (g.label)}
			{#if g.items.length > 0}
				<div class="mt-1 px-1 text-[10px] uppercase tracking-wider text-gray-500">{g.label} · {g.items.length}</div>
				{#each g.items as v (v.actor_id)}
					<div class="mb-1 rounded bg-gray-800/60 px-2 py-1" data-testid="fleet-vehicle">
						<div class="flex items-center gap-2">
							<span class="font-mono text-gray-100">{v.name}</span>
							<span class="rounded px-1.5 py-0.5 text-[10px] font-semibold {toneClass(v.state)}" data-testid="fleet-state">{v.state}</span>
							<span class="ml-auto font-mono text-[10px] text-gray-400">{kmh(v.speed_mps)}</span>
							<button
								type="button"
								onclick={() => despawnFleetVehicle(v.actor_id)}
								class="cursor-pointer px-1 text-gray-500 hover:text-white"
								aria-label="Despawn {v.name}"
							>×</button>
						</div>
						<div class="truncate text-[10px] text-gray-500" title={v.reason}>
							{v.profile}{v.reason ? ` · ${v.reason}` : ''}{v.dist_to_destination_m != null ? ` · ${v.dist_to_destination_m.toFixed(0)} m to go` : ''}
						</div>
						{#if v.kind === 'erv'}
							<div class="mt-1 flex flex-wrap gap-1 text-[10px]">
								<button type="button" onclick={() => toggle(v, 'siren')} aria-pressed={Boolean(v.siren)}
									class="cursor-pointer rounded px-1.5 py-0.5 {v.siren ? 'bg-rose-600 text-white' : 'bg-gray-700 text-gray-300'}">siren</button>
								<button type="button" onclick={() => toggle(v, 'lights')} aria-pressed={Boolean(v.lights)}
									class="cursor-pointer rounded px-1.5 py-0.5 {v.lights ? 'bg-rose-600 text-white' : 'bg-gray-700 text-gray-300'}">lights</button>
								<button type="button" onclick={() => setErvState(v.actor_id, { manual: !v.manual })} aria-pressed={Boolean(v.manual)}
									class="cursor-pointer rounded px-1.5 py-0.5 {v.manual ? 'bg-amber-600 text-white' : 'bg-gray-700 text-gray-300'}"
									title="Hold the vehicle where it is; the siren and lights stay under your control">{v.manual ? 'resume' : 'hold'}</button>
								<button type="button" onclick={() => setErvState(v.actor_id, { arrived: !v.arrived })}
									class="cursor-pointer rounded bg-gray-700 px-1.5 py-0.5 text-gray-300 hover:bg-gray-600"
									title="Park on scene: siren off, lights on, rear doors open">{v.arrived ? 'depart' : 'arrived'}</button>
								<button type="button" onclick={() => setErvState(v.actor_id, { destination: 'random' })}
									class="cursor-pointer rounded bg-gray-700 px-1.5 py-0.5 text-gray-300 hover:bg-gray-600"
									title="Pick a new random destination">new call</button>
							</div>
						{/if}
					</div>
				{/each}
			{/if}
		{/each}

		{#if $fleetTracks.length > 0}
			<div class="mt-1 px-1 text-[10px] uppercase tracking-wider text-gray-500">Emergency vehicles seen · {$fleetTracks.length}</div>
			{#each $fleetTracks as t (t.key)}
				<div class="flex items-center gap-2 px-2 py-0.5 text-[10px]" data-testid="fleet-track">
					<span class="h-2 w-2 rounded-full {t.active ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.8)]' : 'bg-gray-600'}"></span>
					<span class="font-mono text-gray-200">{t.key}</span>
					<span class="text-gray-500">{t.role || 'erv'} · {t.source}{t.stationary ? ' · stationary' : ''}{t.fleet_state ? ` · ${t.fleet_state}` : ''}</span>
					<span class="ml-auto font-mono text-gray-400">{kmh(t.speed_mps)}</span>
				</div>
			{/each}
		{/if}

		{#if recentEvents.length > 0}
			<div class="mt-1 flex items-center justify-between px-1 text-[10px] uppercase tracking-wider text-gray-500">
				<span>Events</span>
				<button type="button" onclick={clearFleetEvents} class="cursor-pointer normal-case tracking-normal text-gray-400 hover:text-white">clear</button>
			</div>
			{#each recentEvents as e, i (e.t + ':' + e.actor_id + ':' + i)}
				<div class="truncate px-2 font-mono text-[10px] text-gray-400" data-testid="fleet-event">
					{fmtTime(e.t)} {e.name} {e.from ?? '·'} → {e.to ?? 'gone'} <span class="text-gray-600">{e.reason}</span>
				</div>
			{/each}
		{/if}
	</div>

	<div class="flex items-center gap-3 border-t border-gray-800 px-3 py-1 text-[10px] text-gray-500">
		{#if $fleetStats}
			<span>{$fleetStats.count}/{$fleetStats.max} vehicles</span>
			<span class={$fleetStats.degraded ? 'text-amber-300' : ''}>tick {$fleetStats.tick_ms_p95.toFixed(1)} ms p95 · {$fleetStats.decision_hz} Hz</span>
		{:else}
			<span>fleet status unknown</span>
		{/if}
	</div>
</div>
