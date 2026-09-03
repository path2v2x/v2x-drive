<script lang="ts">
	import { onMount } from 'svelte';

	interface Props {
		/** Mermaid source; rendered in the browser so the static build stays small. */
		source: string;
		title: string;
	}

	let { source, title }: Props = $props();

	let svg = $state<string | null>(null);
	let error = $state<string | null>(null);
	let renderSerial = 0;

	onMount(async () => {
		const serial = ++renderSerial;
		try {
			const mermaid = (await import('mermaid')).default;
			mermaid.initialize({
				startOnLoad: false,
				theme: 'dark',
				securityLevel: 'strict',
				fontFamily: 'ui-sans-serif, system-ui, sans-serif',
				themeVariables: {
					background: '#030712',
					primaryColor: '#0f172a',
					primaryTextColor: '#e5e7eb',
					primaryBorderColor: '#334155',
					lineColor: '#67e8f9',
					secondaryColor: '#111827',
					tertiaryColor: '#111827',
					clusterBkg: '#0b1220',
					clusterBorder: '#1f2937',
					edgeLabelBackground: '#030712',
					fontSize: '13px'
				},
				flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 28, rankSpacing: 44 }
			});
			const rendered = await mermaid.render(`diagram-${serial}-${Date.now()}`, source);
			if (serial === renderSerial) svg = rendered.svg;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to render diagram.';
		}
	});
</script>

<figure class="overflow-x-auto rounded-3xl border border-gray-800 bg-gray-950 p-4">
	<figcaption class="mb-3 text-[11px] font-medium uppercase tracking-[0.16em] text-gray-500">{title}</figcaption>
	{#if svg}
		<div class="mermaid-host [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-full">{@html svg}</div>
	{:else if error}
		<p class="mb-2 text-xs text-rose-300">{error}</p>
		<pre class="whitespace-pre-wrap font-mono text-xs text-gray-300">{source}</pre>
	{:else}
		<p class="text-xs text-gray-500">Rendering diagram...</p>
	{/if}
</figure>
