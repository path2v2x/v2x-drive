<script lang="ts">
	import FloatingWindow from '$lib/components/FloatingWindow.svelte';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();
</script>

<FloatingWindow id="v2x:test" title="Test window" subtitle="sub" {onClose}>
	{#snippet toolbar()}
		<span data-testid="tb">tb</span>
	{/snippet}
	<p>hello body</p>
</FloatingWindow>
