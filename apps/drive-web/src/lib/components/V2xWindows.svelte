<script lang="ts">
	import { windows } from '$lib/stores/windowManager';
	import { LIST_WINDOW_ID, closeV2xStream, closeV2xViewer, streamKeyOf } from '$lib/stores/v2xViewer';
	import FloatingWindow from './FloatingWindow.svelte';
	import V2xVehicleListPanel from './V2xVehicleListPanel.svelte';
	import V2xStreamWindow from './V2xStreamWindow.svelte';

	let v2xWindows = $derived($windows.filter((w) => w.id.startsWith('v2x:')));
</script>

<!-- Floating layer above the side panels (z-30) and below toasts/modals (z-40+).
     The layer itself lets pointer events through; only the windows catch them. -->
{#if v2xWindows.length > 0}
	<div class="fixed inset-0 z-[35] pointer-events-none" data-testid="v2x-windows">
		{#each v2xWindows as w (w.id)}
			{#if w.id === LIST_WINDOW_ID}
				<FloatingWindow
					id={w.id}
					title="V2X messages"
					subtitle="vehicles with a live J2735 feed"
					accent="text-cyan-300"
					onClose={closeV2xViewer}
					testid="v2x-list-window"
				>
					<V2xVehicleListPanel />
				</FloatingWindow>
			{:else}
				{@const key = streamKeyOf(w.id)}
				{#if key !== null}
					<V2xStreamWindow windowId={w.id} {key} onClose={() => closeV2xStream(key)} />
				{/if}
			{/if}
		{/each}
	</div>
{/if}
