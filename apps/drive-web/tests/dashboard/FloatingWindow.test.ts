import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import FloatingWindowHost from './fixtures/FloatingWindowHost.svelte';
import { loadRect, openWindow, resetWindowManager, windows } from '$lib/stores/windowManager';

beforeEach(() => {
	resetWindowManager();
	localStorage.clear();
});

describe('FloatingWindow', () => {
	it('renders only while its window is open and closes through the header button', async () => {
		const onClose = vi.fn();
		render(FloatingWindowHost, { props: { onClose } });
		expect(screen.queryByRole('dialog')).toBeNull();

		openWindow('v2x:test', { x: 100, y: 50, w: 400, h: 300 });
		const dialog = await screen.findByRole('dialog', { name: 'Test window' });
		expect(dialog).toHaveTextContent('hello body');
		expect(dialog).toHaveTextContent('sub');
		expect(screen.getByTestId('tb')).toBeInTheDocument();
		expect(dialog.style.left).toBe('100px');
		expect(dialog.style.width).toBe('400px');

		await fireEvent.click(screen.getByRole('button', { name: 'Close Test window' }));
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('drags by the header and resizes by the corner, persisting the rect', async () => {
		openWindow('v2x:test', { x: 100, y: 50, w: 400, h: 300 });
		render(FloatingWindowHost, { props: { onClose: () => {} } });
		const header = await screen.findByTestId('window-header');

		await fireEvent.pointerDown(header, { clientX: 150, clientY: 60, button: 0, pointerId: 1 });
		await fireEvent.pointerMove(header, { clientX: 210, clientY: 90, pointerId: 1 });
		await fireEvent.pointerUp(header, { clientX: 210, clientY: 90, pointerId: 1 });
		expect(get(windows)[0].rect).toMatchObject({ x: 160, y: 80 });

		const grip = screen.getByTestId('window-resize');
		await fireEvent.pointerDown(grip, { clientX: 500, clientY: 350, button: 0, pointerId: 2 });
		await fireEvent.pointerMove(grip, { clientX: 560, clientY: 390, pointerId: 2 });
		await fireEvent.pointerUp(grip, { clientX: 560, clientY: 390, pointerId: 2 });
		expect(get(windows)[0].rect).toEqual({ x: 160, y: 80, w: 460, h: 340 });
		expect(loadRect('v2x:test')).toEqual({ x: 160, y: 80, w: 460, h: 340 });

		// clicking a header button never starts a drag
		const close = screen.getByRole('button', { name: 'Close Test window' });
		await fireEvent.pointerDown(close, { clientX: 500, clientY: 60, button: 0, pointerId: 3 });
		await fireEvent.pointerMove(header, { clientX: 100, clientY: 100, pointerId: 3 });
		await fireEvent.pointerUp(header, { clientX: 100, clientY: 100, pointerId: 3 });
		expect(get(windows)[0].rect).toMatchObject({ x: 160, y: 80 });
	});
});
