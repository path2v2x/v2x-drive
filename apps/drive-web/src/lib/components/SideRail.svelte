<script lang="ts">
	// A vertical rail of icon buttons on one side of the camera view: the panel
	// toggles on the left, camera views and session actions on the right.
	import type { RailItem } from '$lib/types';

	interface Props {
		items: RailItem[];
		side: 'left' | 'right';
		/** extra items drawn after a divider (session actions) */
		actions?: RailItem[];
		/** small text under the rail (the bird altitude readout) */
		footer?: string;
	}

	let { items, side, actions = [], footer = '' }: Props = $props();

	function activeClass(item: RailItem): string {
		if (item.active) {
			switch (item.tone) {
				case 'warn':
					return 'bg-amber-600 text-white shadow-[0_0_8px_rgba(217,119,6,0.45)]';
				case 'danger':
					return 'bg-rose-600 text-white shadow-[0_0_8px_rgba(225,29,72,0.45)]';
				case 'ok':
					return 'bg-emerald-600 text-white shadow-[0_0_8px_rgba(5,150,105,0.45)]';
				case 'info':
					return 'bg-cyan-600 text-white shadow-[0_0_8px_rgba(8,145,178,0.45)]';
				default:
					return 'bg-white/15 text-white';
			}
		}
		switch (item.tone) {
			case 'warn':
				return 'bg-orange-600/80 text-white hover:bg-orange-600';
			case 'danger':
				return 'bg-red-600/80 text-white hover:bg-red-600';
			case 'info':
				return 'bg-blue-600/80 text-white hover:bg-blue-600';
			default:
				return 'text-gray-300 hover:bg-white/5 hover:text-white';
		}
	}
</script>

<div
	class="absolute top-1/2 z-20 flex -translate-y-1/2 flex-col gap-0.5 rounded-xl border border-white/10 bg-black/40 p-1 shadow-lg backdrop-blur-md pointer-events-auto {side === 'left' ? 'left-[calc(var(--hud-w,0px)+0.5rem)]' : 'right-2'}"
	data-testid="side-rail-{side}"
	role="toolbar"
	aria-orientation="vertical"
	aria-label={side === 'left' ? 'Panels' : 'Camera and session'}
>
	{#each items as item (item.id)}
		<button
			type="button"
			onclick={item.onClick}
			aria-pressed={item.active ?? false}
			aria-label={item.label}
			title={item.title ?? item.label}
			class="group relative flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg transition-all duration-150 {activeClass(item)}"
		>
			{#if item.icon}
				<svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d={item.icon} />
				</svg>
			{:else}
				<span class="text-[10px] font-semibold tracking-wide">{item.text ?? item.label.slice(0, 4)}</span>
			{/if}
			<span
				class="pointer-events-none absolute top-1/2 hidden -translate-y-1/2 whitespace-nowrap rounded-md bg-gray-900/95 px-2 py-1 text-[11px] font-medium text-gray-100 shadow-lg group-hover:block {side === 'left' ? 'left-full ml-2' : 'right-full mr-2'}"
			>{item.label}</span>
		</button>
	{/each}
	{#if actions.length > 0}
		<span class="my-1 h-px w-full bg-white/15"></span>
		{#each actions as item (item.id)}
			<button
				type="button"
				onclick={item.onClick}
				aria-pressed={item.active ?? false}
				aria-label={item.label}
				title={item.title ?? item.label}
				class="group relative flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg transition-all duration-150 {activeClass(item)}"
			>
				{#if item.icon}
					<svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d={item.icon} />
					</svg>
				{:else}
					<span class="text-[10px] font-semibold tracking-wide">{item.text ?? item.label.slice(0, 4)}</span>
				{/if}
				<span
					class="pointer-events-none absolute top-1/2 hidden -translate-y-1/2 whitespace-nowrap rounded-md bg-gray-900/95 px-2 py-1 text-[11px] font-medium text-gray-100 shadow-lg group-hover:block {side === 'left' ? 'left-full ml-2' : 'right-full mr-2'}"
				>{item.label}</span>
			</button>
		{/each}
	{/if}
	{#if footer}
		<span class="mt-1 text-center font-mono text-[10px] text-gray-300" data-testid="rail-footer">{footer}</span>
	{/if}
</div>
