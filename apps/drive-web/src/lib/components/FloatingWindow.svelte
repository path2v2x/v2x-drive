<script lang="ts">
	import type { Snippet } from 'svelte';
	import {
		WINDOW_MIN_H,
		WINDOW_MIN_W,
		commitWindow,
		focusWindow,
		moveWindow,
		resizeWindow,
		windows
	} from '$lib/stores/windowManager';

	interface Props {
		/** Window id registered with the window manager (openWindow). */
		id: string;
		title: string;
		subtitle?: string;
		/** Tailwind text color class for the title. */
		accent?: string;
		onClose: () => void;
		children: Snippet;
		/** Extra header content (rendered left of the close button). */
		toolbar?: Snippet;
		testid?: string;
	}

	let {
		id,
		title,
		subtitle = '',
		accent = 'text-cyan-300',
		onClose,
		children,
		toolbar,
		testid = 'floating-window'
	}: Props = $props();

	let win = $derived($windows.find((w) => w.id === id));

	let drag: { pointerId: number; dx: number; dy: number } | null = null;
	let resize: { pointerId: number; x0: number; y0: number; w0: number; h0: number } | null = null;

	function capture(target: EventTarget | null, pointerId: number): void {
		const el = target as HTMLElement | null;
		try {
			el?.setPointerCapture?.(pointerId);
		} catch {
			/* not supported (jsdom) */
		}
	}

	function release(target: EventTarget | null, pointerId: number): void {
		const el = target as HTMLElement | null;
		try {
			el?.releasePointerCapture?.(pointerId);
		} catch {
			/* not supported */
		}
	}

	function onHeaderDown(e: PointerEvent): void {
		if (!win || e.button !== 0) return;
		// Header buttons keep working; only the free header area drags.
		if ((e.target as HTMLElement | null)?.closest('button, input, select, a')) return;
		focusWindow(id);
		drag = { pointerId: e.pointerId, dx: e.clientX - win.rect.x, dy: e.clientY - win.rect.y };
		capture(e.currentTarget, e.pointerId);
		e.preventDefault();
	}

	function onHeaderMove(e: PointerEvent): void {
		if (!drag || e.pointerId !== drag.pointerId) return;
		moveWindow(id, e.clientX - drag.dx, e.clientY - drag.dy);
	}

	function onHeaderUp(e: PointerEvent): void {
		if (!drag || e.pointerId !== drag.pointerId) return;
		release(e.currentTarget, e.pointerId);
		drag = null;
		commitWindow(id);
	}

	function onResizeDown(e: PointerEvent): void {
		if (!win || e.button !== 0) return;
		focusWindow(id);
		resize = { pointerId: e.pointerId, x0: e.clientX, y0: e.clientY, w0: win.rect.w, h0: win.rect.h };
		capture(e.currentTarget, e.pointerId);
		e.preventDefault();
		e.stopPropagation();
	}

	function onResizeMove(e: PointerEvent): void {
		if (!resize || e.pointerId !== resize.pointerId) return;
		resizeWindow(id, resize.w0 + (e.clientX - resize.x0), resize.h0 + (e.clientY - resize.y0));
	}

	function onResizeUp(e: PointerEvent): void {
		if (!resize || e.pointerId !== resize.pointerId) return;
		release(e.currentTarget, e.pointerId);
		resize = null;
		commitWindow(id);
	}
</script>

{#if win}
	<div
		role="dialog"
		tabindex="-1"
		aria-label={title}
		data-testid={testid}
		data-window-id={id}
		class="absolute flex flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-900/95 text-gray-200 shadow-2xl shadow-black/60 backdrop-blur-md pointer-events-auto"
		style="left:{win.rect.x}px; top:{win.rect.y}px; width:{win.rect.w}px; height:{win.rect.h}px; z-index:{win.z}; min-width:{WINDOW_MIN_W}px; min-height:{WINDOW_MIN_H}px;"
		onpointerdown={() => focusWindow(id)}
	>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="flex cursor-move touch-none select-none items-center gap-2 border-b border-gray-700 bg-gray-800/70 px-3 py-1.5"
			data-testid="window-header"
			onpointerdown={onHeaderDown}
			onpointermove={onHeaderMove}
			onpointerup={onHeaderUp}
			onpointercancel={onHeaderUp}
		>
			<div class="min-w-0 flex-1 leading-tight">
				<div class="truncate text-sm font-semibold tracking-wide {accent}">{title}</div>
				{#if subtitle}
					<div class="truncate text-[10px] text-gray-500">{subtitle}</div>
				{/if}
			</div>
			{#if toolbar}
				{@render toolbar()}
			{/if}
			<button
				type="button"
				onclick={onClose}
				class="cursor-pointer px-1 text-xl leading-none text-gray-400 hover:text-white"
				aria-label="Close {title}"
			>×</button>
		</div>

		<div class="flex min-h-0 flex-1 flex-col">
			{@render children()}
		</div>

		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="absolute bottom-0 right-0 h-4 w-4 cursor-nwse-resize touch-none"
			data-testid="window-resize"
			aria-hidden="true"
			onpointerdown={onResizeDown}
			onpointermove={onResizeMove}
			onpointerup={onResizeUp}
			onpointercancel={onResizeUp}
		>
			<svg viewBox="0 0 16 16" class="h-4 w-4 text-gray-500" aria-hidden="true">
				<path d="M15 2 2 15M15 9l-6 6" stroke="currentColor" stroke-width="1.5" fill="none" />
			</svg>
		</div>
	</div>
{/if}
