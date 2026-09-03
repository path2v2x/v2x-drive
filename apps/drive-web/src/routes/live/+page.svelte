<script lang="ts">
	import { onMount } from 'svelte';
	import Header from '$lib/components/Header.svelte';
	import LiveVideoCard from '$lib/components/LiveVideoCard.svelte';
	import RecentDetectionsPanel from '$lib/components/RecentDetectionsPanel.svelte';
	import { loadRuntimeConfig, type RuntimeConfig } from '$lib/runtime-config';

	let runtimeConfig = $state<RuntimeConfig | null>(null);
	let cameraIds = $derived(runtimeConfig?.videoCameraIds ?? ['ch1', 'ch2', 'ch3', 'ch4']);

	onMount(async () => {
		runtimeConfig = await loadRuntimeConfig();
	});
</script>

<svelte:head>
	<title>V2X Drive Street View</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950">
	<Header />

	<div class="min-h-0 flex-1 overflow-y-auto bg-black">
		<div
			class="grid gap-px bg-gray-900"
			style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));"
		>
			{#each cameraIds as cameraId (cameraId)}
				<LiveVideoCard
					{cameraId}
					liveVideoUrlTemplate={runtimeConfig?.liveVideoUrlTemplate || ''}
				/>
			{/each}
		</div>
		<RecentDetectionsPanel limit={50} />
	</div>
</div>
