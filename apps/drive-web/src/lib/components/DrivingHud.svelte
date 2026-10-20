<script lang="ts">
	// The pygame manual_control.py HUD: a translucent column on the left of the
	// camera with the telemetry in that script's line order.
	import type { Snippet } from 'svelte';
	import type { VehicleTelemetry } from '$lib/types';
	import {
		CollisionHistory,
		collisionPolyline,
		compassLabel,
		fixed,
		formatSimTime,
		formatVector,
		gearLabel,
		nearbyRows
	} from '$lib/hudFormat';

	interface Props {
		telemetry: VehicleTelemetry;
		clientFps?: number;
		inputMode?: 'keyboard' | 'wheel';
		connected?: boolean;
		/** Interactive block docked under the telemetry (the V2X perspective). */
		children?: Snippet;
	}

	let { telemetry, clientFps = 0, inputMode = 'keyboard', connected = true, children }: Props = $props();

	const history = new CollisionHistory(200);
	let polyline = $state('');

	// one sample per telemetry frame, like manual_control.py's collision history
	// (the effect only reads the telemetry prop and writes the polyline, never the reverse)
	$effect(() => {
		const t = telemetry;
		history.push(t.hud?.collision?.intensity ?? 0);
		polyline = collisionPolyline(history.normalized(), 100, 20);
	});

	let hud = $derived(telemetry.hud);
	let nearby = $derived(nearbyRows(telemetry.pos, telemetry.nearby_actors, 8));
	let throttlePct = $derived(Math.max(0, Math.min(1, telemetry.throttle)));
	let brakePct = $derived(Math.max(0, Math.min(1, telemetry.brake)));
	let steerPct = $derived((Math.max(-1, Math.min(1, telemetry.steer)) + 1) / 2);
	let reverse = $derived(telemetry.gear < 0);
	let speedLine = $derived(
		`${String(Math.round(telemetry.speed)).padStart(4, ' ')} km/h` +
			(hud?.speed_limit ? `  (limit ${Math.round(hud.speed_limit)})` : '')
	);
	let compassLine = $derived(
		hud ? `${String(Math.round(hud.compass)).padStart(4, ' ')}° ${compassLabel(hud.compass)}` : '-'
	);
	let gnssLine = $derived(hud?.gnss ? `(${hud.gnss[0].toFixed(6)}, ${hud.gnss[1].toFixed(6)})` : '(-, -)');
	let locationLine = $derived(`(${fixed(telemetry.pos[0], 7, 1)},${fixed(telemetry.pos[1], 7, 1)})`);
	let lastHit = $derived(hud?.collision ? `${hud.collision.with} (${hud.collision.intensity.toFixed(0)})` : '');
</script>

<aside
	class="absolute inset-y-0 left-0 z-20 flex w-[300px] max-w-[46vw] flex-col overflow-hidden bg-black/40 px-3 py-2 font-mono text-[12px] leading-[18px] text-gray-100 select-none pointer-events-none backdrop-blur-[2px]"
	data-testid="driving-hud"
	aria-label="Driving HUD"
>
	<div class="flex justify-between"><span class="text-gray-400">Server:</span><span data-testid="hud-server-fps">{hud ? Math.round(hud.server_fps) : '-'} FPS</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Client:</span><span data-testid="hud-client-fps">{Math.round(clientFps)} FPS</span></div>
	<div class="h-[9px]"></div>
	<div class="flex justify-between"><span class="text-gray-400">Vehicle:</span><span class="truncate pl-2" data-testid="hud-vehicle">{hud?.vehicle || '-'}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Map:</span><span class="truncate pl-2" data-testid="hud-map">{hud?.map || '-'}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Simulation time:</span><span data-testid="hud-sim-time">{formatSimTime(hud?.sim_time)}</span></div>
	<div class="h-[9px]"></div>
	<div class="flex justify-between"><span class="text-gray-400">Speed:</span><span class="whitespace-pre" data-testid="hud-speed">{speedLine}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Compass:</span><span class="whitespace-pre" data-testid="hud-compass">{compassLine}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Accelero:</span><span class="whitespace-pre text-[11px]" data-testid="hud-accel">{formatVector(hud?.accel)}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Gyroscop:</span><span class="whitespace-pre text-[11px]" data-testid="hud-gyro">{formatVector(hud?.gyro)}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Location:</span><span class="whitespace-pre" data-testid="hud-location">{locationLine}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">GNSS:</span><span class="text-[11px]" data-testid="hud-gnss">{gnssLine}</span></div>
	<div class="flex justify-between"><span class="text-gray-400">Height:</span><span data-testid="hud-height">{hud ? `${Math.round(hud.height)} m` : '-'}</span></div>
	<div class="h-[9px]"></div>

	<!-- throttle / steer / brake bars, as manual_control.py draws them -->
	<div class="flex items-center gap-2"><span class="w-[8ch] text-gray-400">Throttle:</span>
		<div class="relative h-[6px] flex-1 border border-white/70"><div class="absolute inset-y-0 left-0 bg-white" style="width: {throttlePct * 100}%"></div></div>
	</div>
	<div class="flex items-center gap-2"><span class="w-[8ch] text-gray-400">Steer:</span>
		<div class="relative h-[6px] flex-1 border border-white/70"><div class="absolute inset-y-0 w-[6px] -translate-x-1/2 bg-white" style="left: {steerPct * 100}%"></div></div>
	</div>
	<div class="flex items-center gap-2"><span class="w-[8ch] text-gray-400">Brake:</span>
		<div class="relative h-[6px] flex-1 border border-white/70"><div class="absolute inset-y-0 left-0 bg-white" style="width: {brakePct * 100}%"></div></div>
	</div>
	<div class="flex items-center gap-2"><span class="w-[10ch] text-gray-400">Reverse:</span><span class="inline-block h-[10px] w-[10px] border border-white/70 {reverse ? 'bg-white' : ''}" data-testid="hud-reverse" aria-checked={reverse} role="checkbox"></span></div>
	<div class="flex items-center gap-2"><span class="w-[10ch] text-gray-400">Hand brake:</span><span class="inline-block h-[10px] w-[10px] border border-white/70 {hud?.hand_brake ? 'bg-white' : ''}" aria-checked={Boolean(hud?.hand_brake)} role="checkbox"></span></div>
	<div class="flex items-center gap-2"><span class="w-[10ch] text-gray-400">Manual:</span><span class="inline-block h-[10px] w-[10px] border border-white/70 {hud?.manual ? 'bg-white' : ''}" aria-checked={Boolean(hud?.manual)} role="checkbox"></span></div>
	<div class="flex justify-between"><span class="text-gray-400">Gear:</span><span data-testid="hud-gear">{gearLabel(telemetry.gear)}</span></div>
	<div class="h-[9px]"></div>

	<div class="text-gray-400">Collision:</div>
	<svg viewBox="0 0 100 20" preserveAspectRatio="none" class="h-7 w-full" aria-label="collision history">
		<polyline points={polyline} fill="none" stroke="white" stroke-width="0.8" vector-effect="non-scaling-stroke" />
	</svg>
	{#if lastHit}
		<div class="truncate text-[11px] text-amber-300" data-testid="hud-collision">hit {lastHit}</div>
	{/if}
	<div class="h-[9px]"></div>

	<div class="flex justify-between"><span class="text-gray-400">Number of vehicles:</span><span data-testid="hud-vehicle-count">{hud?.vehicles ?? '-'}</span></div>
	<div class="text-gray-400">Nearby vehicles:</div>
	{#each nearby as row (row.id)}
		<div class="truncate whitespace-pre" data-testid="hud-nearby">{String(Math.round(row.dist)).padStart(4, ' ')} m  {row.name}</div>
	{:else}
		<div class="text-gray-500">none within 200 m</div>
	{/each}

	{#if children}
		<div class="mt-2 min-h-0 flex-1 overflow-y-auto border-t border-white/10 pt-2 pointer-events-auto select-text" data-testid="hud-dock">
			{@render children()}
		</div>
	{/if}

	<div class="mt-auto flex shrink-0 items-center justify-between pt-1 text-[10px] text-gray-400">
		<span><span class="inline-block h-1.5 w-1.5 rounded-full {connected ? 'bg-green-400' : 'bg-red-500'}"></span> {inputMode === 'wheel' ? 'Wheel' : 'WASD'}</span>
		<span>H: hide</span>
	</div>
</aside>
