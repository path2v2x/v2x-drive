<script lang="ts">
	// Names floating above the actors in the camera image.  The server sends, with every
	// telemetry tick, the camera's world pose and image size plus each nearby actor's position
	// and height; each label is projected to the pixel above the actor's roof and placed over
	// the MJPEG frame, which is letterboxed (object-contain) inside this same box.
	import type { CameraInfo, NearbyActor } from '$lib/types';
	import { frameRect, projectPoint } from '$lib/projection';

	interface Props {
		actors: NearbyActor[];
		camera: CameraInfo | null;
		/** labels beyond this distance are not drawn (m) */
		maxDistance?: number;
		maxLabels?: number;
	}
	let { actors, camera, maxDistance = 250, maxLabels = 40 }: Props = $props();

	let boxW = $state(0);
	let boxH = $state(0);

	interface Label {
		id: number;
		name: string;
		x: number;
		y: number;
		depth: number;
		type: string;
	}

	let labels = $derived.by((): Label[] => {
		if (!camera || boxW < 10 || boxH < 10) return [];
		const rect = frameRect(boxW, boxH, camera.w, camera.h);
		const out: Label[] = [];
		for (const a of actors) {
			if (!a.name || typeof a.z !== 'number') continue;
			const p = projectPoint(camera, a.pos[0], a.pos[1], a.z + (a.h ?? 1.6) + 0.4);
			if (!p || p.depth > maxDistance) continue;
			if (p.u < -40 || p.u > camera.w + 40 || p.v < -20 || p.v > camera.h + 20) continue;
			out.push({
				id: a.id,
				name: a.name,
				x: rect.left + p.u * rect.scale,
				y: rect.top + p.v * rect.scale,
				depth: p.depth,
				type: a.type,
			});
		}
		out.sort((a, b) => a.depth - b.depth);
		return out.slice(0, maxLabels);
	});

	const COLORS: Record<string, string> = {
		erv: '#fda4af',
		civilian: '#7dd3fc',
		dynamic: '#fca5a5',
		traffic: '#fcd34d',
	};
</script>

<div class="pointer-events-none absolute inset-0 select-none overflow-hidden" bind:clientWidth={boxW} bind:clientHeight={boxH}>
	{#each labels as l (l.id)}
		<div
			class="absolute -translate-x-1/2 -translate-y-full whitespace-nowrap rounded border border-white/20 bg-black/60 px-1.5 py-0.5 font-mono font-semibold leading-none"
			style="left: {l.x}px; top: {l.y}px; font-size: {l.depth < 40 ? 11 : 10}px; opacity: {l.depth < 80 ? 1 : Math.max(0.45, 1 - (l.depth - 80) / 250)}; color: {COLORS[l.type] ?? '#f8fafc'};"
		>
			{l.name}
		</div>
	{/each}
</div>
