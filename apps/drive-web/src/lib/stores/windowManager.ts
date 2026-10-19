/**
 * Floating window manager — position, size, z-order and persistence for the
 * draggable panels (the V2X message viewer windows). Pure store logic; the
 * FloatingWindow component renders one entry and calls back into this module.
 */

import { writable, get } from 'svelte/store';

export interface WindowRect {
	x: number;
	y: number;
	w: number;
	h: number;
}

export interface WindowState {
	id: string;
	rect: WindowRect;
	z: number;
}

export const WINDOW_MIN_W = 320;
export const WINDOW_MIN_H = 220;
const STORAGE_PREFIX = 'v2x-window:';
const CASCADE_STEP = 28;
const CASCADE_WRAP = 10;
// At least this much of a window stays on screen horizontally, and the title
// bar always stays reachable vertically, so a window can never be lost.
const VISIBLE_MARGIN = 80;
const TITLE_BAR = 40;

export const windows = writable<WindowState[]>([]);
let zTop = 0;
let cascade = 0;

function viewport(): { w: number; h: number } {
	if (typeof window === 'undefined') return { w: 1280, h: 800 };
	return { w: window.innerWidth || 1280, h: window.innerHeight || 800 };
}

export function clampRect(rect: WindowRect, vp: { w: number; h: number } = viewport()): WindowRect {
	const w = Math.max(WINDOW_MIN_W, Math.min(rect.w, vp.w));
	const h = Math.max(WINDOW_MIN_H, Math.min(rect.h, vp.h));
	const x = Math.max(VISIBLE_MARGIN - w, Math.min(rect.x, vp.w - VISIBLE_MARGIN));
	const y = Math.max(0, Math.min(rect.y, vp.h - TITLE_BAR));
	return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

function isRect(value: unknown): value is WindowRect {
	if (typeof value !== 'object' || value === null) return false;
	const v = value as Record<string, unknown>;
	return ['x', 'y', 'w', 'h'].every((k) => typeof v[k] === 'number' && Number.isFinite(v[k]));
}

export function loadRect(id: string): WindowRect | null {
	try {
		const raw = localStorage.getItem(STORAGE_PREFIX + id);
		if (!raw) return null;
		const parsed: unknown = JSON.parse(raw);
		return isRect(parsed) ? clampRect(parsed) : null;
	} catch {
		return null;
	}
}

export function saveRect(id: string, rect: WindowRect): void {
	try {
		localStorage.setItem(STORAGE_PREFIX + id, JSON.stringify(rect));
	} catch {
		/* storage unavailable or full */
	}
}

function cascadeRect(defaults: Partial<WindowRect>): WindowRect {
	const n = cascade % CASCADE_WRAP;
	cascade += 1;
	return clampRect({
		x: defaults.x ?? 72 + n * CASCADE_STEP,
		y: defaults.y ?? 72 + n * CASCADE_STEP,
		w: defaults.w ?? 460,
		h: defaults.h ?? 360
	});
}

export function isWindowOpen(id: string): boolean {
	return get(windows).some((w) => w.id === id);
}

export function topWindowId(): string | null {
	let top: WindowState | null = null;
	for (const w of get(windows)) {
		if (!top || w.z > top.z) top = w;
	}
	return top ? top.id : null;
}

export function focusWindow(id: string): void {
	const list = get(windows);
	const win = list.find((w) => w.id === id);
	if (!win) return;
	if (list.every((w) => w.z <= win.z)) return; // already on top
	zTop += 1;
	const z = zTop;
	windows.update((items) => items.map((w) => (w.id === id ? { ...w, z } : w)));
}

/** Open (or focus) a window; a stored rect wins over the defaults, else windows cascade. */
export function openWindow(id: string, defaults: Partial<WindowRect> = {}): WindowState {
	const existing = get(windows).find((w) => w.id === id);
	if (existing) {
		focusWindow(id);
		return get(windows).find((w) => w.id === id) ?? existing;
	}
	zTop += 1;
	const state: WindowState = { id, rect: loadRect(id) ?? cascadeRect(defaults), z: zTop };
	windows.update((items) => [...items, state]);
	return state;
}

export function closeWindow(id: string): void {
	windows.update((items) => items.filter((w) => w.id !== id));
}

export function closeAllWindows(prefix = ''): void {
	windows.update((items) => items.filter((w) => !w.id.startsWith(prefix)));
}

export function moveWindow(id: string, x: number, y: number): void {
	windows.update((items) =>
		items.map((w) => (w.id === id ? { ...w, rect: clampRect({ ...w.rect, x, y }) } : w))
	);
}

export function resizeWindow(id: string, w: number, h: number): void {
	windows.update((items) =>
		items.map((win) => (win.id === id ? { ...win, rect: clampRect({ ...win.rect, w, h }) } : win))
	);
}

/** Persist the current rect (called when a drag or resize ends). */
export function commitWindow(id: string): void {
	const win = get(windows).find((w) => w.id === id);
	if (win) saveRect(id, win.rect);
}

/** Test helper: forget every window and the z/cascade counters. */
export function resetWindowManager(): void {
	windows.set([]);
	zTop = 0;
	cascade = 0;
}
