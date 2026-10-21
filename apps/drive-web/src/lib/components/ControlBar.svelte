<script lang="ts">
	// The controls of the driving view: every button named and grouped (panels, camera
	// views, session actions), rendered as a block at the bottom of the HUD column.
	import type { RailItem } from '$lib/types';

	export interface ControlSection {
		title: string;
		items: RailItem[];
		/** small line under the section (the bird altitude readout) */
		note?: string;
	}

	interface Props {
		sections: ControlSection[];
		footer?: string;
	}

	let { sections, footer = '' }: Props = $props();

	function toneText(item: RailItem): string {
		switch (item.tone) {
			case 'warn':
				return 'text-orange-300';
			case 'danger':
				return 'text-red-300';
			case 'ok':
				return 'text-emerald-300';
			case 'info':
			default:
				return 'text-gray-200';
		}
	}
</script>

<nav class="mt-auto flex flex-col border-t border-white/10 pt-1" data-testid="control-bar" aria-label="Controls">
	{#each sections as section (section.title)}
		<div class="px-2 pb-0.5 pt-2 text-[10px] uppercase tracking-widest text-gray-500">{section.title}</div>
		{#each section.items as item (item.id)}
			<button
				type="button"
				onclick={item.onClick}
				aria-pressed={item.active ?? false}
				title={item.title ?? item.label}
				class="flex w-full cursor-pointer items-center gap-2 rounded px-2 py-[3px] text-left transition-colors duration-100 {item.active
					? 'bg-white/15 text-white'
					: `${toneText(item)} hover:bg-white/10 hover:text-white`}"
			>
				{#if item.icon}
					<svg class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d={item.icon} />
					</svg>
				{:else}
					<span class="inline-block h-1.5 w-1.5 shrink-0 rounded-full {item.active ? 'bg-white' : 'bg-gray-500'}" aria-hidden="true"></span>
				{/if}
				<span class="truncate">{item.label}</span>
			</button>
		{/each}
		{#if section.note}
			<div class="px-2 text-[10px] text-gray-400" data-testid="control-note">{section.note}</div>
		{/if}
	{/each}
	{#if footer}
		<div class="px-2 pt-3 text-[10px] text-gray-500" data-testid="control-footer">{footer}</div>
	{/if}
</nav>
