<script lang="ts">
	// What the ego's onboard unit tells the driver, derived from the BSMs it hears inside the
	// reception range: not the BSM fields (that is the V2X panel above), but the advice built
	// from them.  Today: pull over when an emergency vehicle comes up from behind.
	import { onDestroy, onMount } from 'svelte';
	import { telemetry } from '$lib/stores/driveSocket';
	import { v2xPerspective, v2xRadiusM } from '$lib/stores/v2xPerspective';
	import { deriveV2xMessages, type SenderTrack, type V2xMessage } from '$lib/v2xMessages';

	// state across snapshots: last distance per sender, messages with their last-true time
	const tracks = new Map<string, SenderTrack>();
	const active = new Map<string, V2xMessage & { lastTrue: number }>();

	// a clock so a held message can expire without a new snapshot
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;
	onMount(() => {
		timer = setInterval(() => {
			now = Date.now();
		}, 500);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	let messages = $derived.by((): V2xMessage[] => {
		const t = $telemetry;
		const p = $v2xPerspective;
		const clock = now;
		if (!p) return [];
		return deriveV2xMessages({ x: t.pos[0], y: t.pos[1], yaw: t.rot[1] }, p.vehicles, $v2xRadiusM, tracks, active, clock);
	});

	function age(m: V2xMessage): string {
		const s = Math.max(0, Math.round((now - m.since) / 1000));
		return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
	}
</script>

<section class="mt-3 border-t border-white/10 pt-2 font-mono text-[12px] leading-[18px] text-gray-100" data-testid="v2x-messages">
	<div class="flex items-center gap-x-2 pb-1">
		<span class="shrink-0 whitespace-nowrap text-[10px] uppercase tracking-widest text-gray-500">V2X messages · {messages.length}</span>
		<span class="ml-auto text-[10px] text-gray-500" title="Messages are built from the BSMs heard inside the reception range set above">within {$v2xRadiusM} m</span>
	</div>
	{#each messages as m (m.id)}
		<div class="mb-1 rounded border border-red-400/60 bg-red-600/25 px-2 py-1" data-testid="v2x-message" role="alert">
			<div class="flex items-center gap-2">
				<span class="text-red-300" aria-hidden="true">&#9888;</span>
				<span class="font-semibold tracking-wider text-red-200">{m.title}</span>
				<span class="ml-auto shrink-0 text-[10px] text-gray-300">{Math.round(m.distance_m)} m · behind · {age(m)}</span>
			</div>
			<div class="text-[11px] leading-[15px] text-gray-100">{m.text}</div>
		</div>
	{:else}
		<div class="text-gray-500">no messages</div>
	{/each}
</section>
