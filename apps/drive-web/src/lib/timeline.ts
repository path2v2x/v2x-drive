import type { CoverageBucket, DetectionObject } from './types';

/** Length of one archive playback window requested from MediaMTX. */
export const PLAYBACK_WINDOW_MS = 15 * 60 * 1000;

/** The twin server keeps 72 h of detections; MediaMTX keeps 72 h of video. */
export const TIMELINE_SPAN_MS = 72 * 60 * 60 * 1000;

/** Maximum tolerated wall-clock skew between an archive pane and replay. */
export const ARCHIVE_MAX_CURSOR_DRIFT_MS = 250;

/** Coverage buckets are requested per view so the histogram stays legible when zoomed. */
export const MAX_COVERAGE_BUCKETS = 720;
export const MIN_COVERAGE_BUCKET_SECONDS = 10;

export function coverageBucketSecondsForSpan(spanMs: number): number {
	const raw = Math.ceil(spanMs / 1000 / MAX_COVERAGE_BUCKETS);
	return Math.max(MIN_COVERAGE_BUCKET_SECONDS, Math.ceil(raw / 10) * 10);
}

/** Media time inside a clip for a wall-clock instant; clamps to the clip start. */
export function archiveMediaTimeForEpoch(epochMs: number, clipStartMs: number): number {
	return Math.max(0, (epochMs - clipStartMs) / 1000);
}

/** Map native archive MP4 media time to the recording's wall clock. */
export function archiveEpochForMediaTime(clipStartMs: number, mediaTimeSeconds: number): number {
	return clipStartMs + mediaTimeSeconds * 1000;
}

export function archiveCursorNeedsCorrection(
	cursorMs: number,
	currentEpochMs: number,
	maxDriftMs = ARCHIVE_MAX_CURSOR_DRIFT_MS
): boolean {
	return (
		Number.isFinite(cursorMs) &&
		Number.isFinite(currentEpochMs) &&
		Number.isFinite(maxDriftMs) &&
		maxDriftMs >= 0 &&
		Math.abs(cursorMs - currentEpochMs) > maxDriftMs
	);
}

export const OBJECT_TYPE_COLORS: Record<string, string> = {
	car: '#38bdf8',
	truck: '#facc15',
	bus: '#c084fc',
	person: '#4ade80',
	default: '#f87171'
};

export function objectTypeColor(objectType: string): string {
	return OBJECT_TYPE_COLORS[objectType] ?? OBJECT_TYPE_COLORS.default;
}

export function toIsoMillis(epochMs: number): string {
	return new Date(epochMs).toISOString().replace(/(\.\d{3})\d*Z$/, '$1Z');
}

export function parseIsoMs(value: string | null | undefined): number | null {
	if (!value) return null;
	const ms = Date.parse(value);
	return Number.isNaN(ms) ? null : ms;
}

export interface PlaybackWindow {
	startMs: number;
	endMs: number;
	start: string;
	end: string;
}

/**
 * Compute the playback window containing `cursorMs`. Windows are aligned to
 * fixed boundaries so scrubbing back and forth reuses the same HLS session.
 */
export function windowForCursor(cursorMs: number, nowMs: number): PlaybackWindow {
	let startMs = Math.floor(cursorMs / PLAYBACK_WINDOW_MS) * PLAYBACK_WINDOW_MS;
	let endMs = startMs + PLAYBACK_WINDOW_MS;
	if (endMs > nowMs) {
		endMs = nowMs;
		startMs = Math.max(endMs - PLAYBACK_WINDOW_MS, nowMs - TIMELINE_SPAN_MS);
	}
	return { startMs, endMs, start: toIsoMillis(startMs), end: toIsoMillis(endMs) };
}

export interface MarkerLayout {
	event: DetectionObject;
	x: number; // 0..1 fraction across the visible span
	color: string;
}

export function layoutMarkers(
	events: DetectionObject[],
	viewStartMs: number,
	viewEndMs: number
): MarkerLayout[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const markers: MarkerLayout[] = [];
	for (const event of events) {
		const t = parseIsoMs(event.first_seen);
		if (t === null || t < viewStartMs || t > viewEndMs) continue;
		markers.push({
			event,
			x: (t - viewStartMs) / span,
			color: objectTypeColor(event.object_type)
		});
	}
	return markers;
}


export interface HistogramBarLayout {
	x: number;
	width: number;
	total: number;
	intensity: number; // 0..1 relative to the max bucket in view
}

export function layoutHistogram(
	buckets: CoverageBucket[],
	bucketSeconds: number,
	viewStartMs: number,
	viewEndMs: number
): HistogramBarLayout[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const bucketMs = bucketSeconds * 1000;
	const visible: { x: number; width: number; total: number }[] = [];
	let max = 0;
	for (const bucket of buckets) {
		const s = parseIsoMs(bucket.start);
		if (s === null || s + bucketMs < viewStartMs || s > viewEndMs) continue;
		const total = bucket.detections;
		if (total > max) max = total;
		visible.push({
			x: (Math.max(s, viewStartMs) - viewStartMs) / span,
			width: bucketMs / span,
			total
		});
	}
	if (max === 0) return [];
	return visible.map((bar) => ({ ...bar, intensity: bar.total / max }));
}


export function formatClock(epochMs: number): string {
	return new Date(epochMs).toLocaleTimeString([], {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit'
	});
}

export function formatShortClock(epochMs: number): string {
	return new Date(epochMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Evenly spaced tick marks for the visible span. */
export function timeTicks(
	viewStartMs: number,
	viewEndMs: number,
	targetCount = 8
): { x: number; label: string }[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const steps = [
		60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000,
		3_600_000, 2 * 3_600_000, 3 * 3_600_000, 6 * 3_600_000, 12 * 3_600_000
	];
	const step = steps.find((s) => span / s <= targetCount) ?? steps[steps.length - 1];
	const ticks: { x: number; label: string }[] = [];
	for (let t = Math.ceil(viewStartMs / step) * step; t <= viewEndMs; t += step) {
		ticks.push({ x: (t - viewStartMs) / span, label: formatShortClock(t) });
	}
	return ticks;
}
