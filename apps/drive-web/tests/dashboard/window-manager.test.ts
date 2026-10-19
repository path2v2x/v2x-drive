import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	WINDOW_MIN_H,
	WINDOW_MIN_W,
	clampRect,
	closeAllWindows,
	closeWindow,
	commitWindow,
	focusWindow,
	isWindowOpen,
	loadRect,
	moveWindow,
	openWindow,
	resetWindowManager,
	resizeWindow,
	topWindowId,
	windows
} from '$lib/stores/windowManager';

beforeEach(() => {
	resetWindowManager();
	localStorage.clear();
});

describe('window manager', () => {
	it('clamps rects to the viewport and the minimum size', () => {
		const vp = { w: 1000, h: 600 };
		expect(clampRect({ x: -500, y: -50, w: 100, h: 50 }, vp)).toEqual({
			x: 80 - WINDOW_MIN_W,
			y: 0,
			w: WINDOW_MIN_W,
			h: WINDOW_MIN_H
		});
		expect(clampRect({ x: 5000, y: 5000, w: 5000, h: 5000 }, vp)).toEqual({ x: 920, y: 560, w: 1000, h: 600 });
		expect(clampRect({ x: 10.4, y: 20.6, w: 400, h: 300 }, vp)).toEqual({ x: 10, y: 21, w: 400, h: 300 });
	});

	it('cascades new windows, keeps z-order on focus and reopens on top', () => {
		const a = openWindow('v2x:a');
		const b = openWindow('v2x:b');
		expect(b.rect.x).toBe(a.rect.x + 28);
		expect(b.rect.y).toBe(a.rect.y + 28);
		expect(b.z).toBeGreaterThan(a.z);
		expect(topWindowId()).toBe('v2x:b');

		focusWindow('v2x:a');
		expect(topWindowId()).toBe('v2x:a');
		focusWindow('v2x:a'); // already on top: no change
		expect(get(windows).find((w) => w.id === 'v2x:a')?.z).toBe(3);

		openWindow('v2x:b'); // existing window: focus instead of a duplicate
		expect(topWindowId()).toBe('v2x:b');
		expect(get(windows)).toHaveLength(2);
		expect(isWindowOpen('v2x:a')).toBe(true);
		expect(isWindowOpen('v2x:zzz')).toBe(false);
	});

	it('persists moves and resizes and restores them on reopen', () => {
		openWindow('v2x:a');
		moveWindow('v2x:a', 300, 200);
		resizeWindow('v2x:a', 640, 480);
		expect(loadRect('v2x:a')).toBeNull(); // nothing stored until the gesture ends
		commitWindow('v2x:a');
		expect(loadRect('v2x:a')).toEqual({ x: 300, y: 200, w: 640, h: 480 });

		closeWindow('v2x:a');
		expect(get(windows)).toEqual([]);
		const again = openWindow('v2x:a', { x: 1, y: 1, w: 400, h: 300 });
		expect(again.rect).toEqual({ x: 300, y: 200, w: 640, h: 480 });

		openWindow('v2x:b');
		openWindow('other:c');
		closeAllWindows('v2x:');
		expect(get(windows).map((w) => w.id)).toEqual(['other:c']);
		closeAllWindows();
		expect(get(windows)).toEqual([]);
		expect(topWindowId()).toBeNull();
	});

	it('ignores corrupt stored rects', () => {
		localStorage.setItem('v2x-window:x', '{"x":"a","y":1,"w":2,"h":3}');
		expect(loadRect('x')).toBeNull();
		localStorage.setItem('v2x-window:x', 'not json');
		expect(loadRect('x')).toBeNull();
		localStorage.setItem('v2x-window:x', '{"x":-9999,"y":-9999,"w":10,"h":10}');
		const rect = loadRect('x');
		expect(rect?.w).toBe(WINDOW_MIN_W);
		expect(rect?.y).toBe(0);
	});
});
