import { buildAssetUrl, loadRuntimeConfig } from './runtime-config';
import type {
	DemoVideo,
	DetectionCoverage,
	DetectionHistoryPage,
	DetectionObject,
	TrackedObject
} from './types';

export interface StateJson {
	objects: TrackedObject[];
	bridge_status?: {
		status?: string | null;
		carla_fps?: number | null;
		objects_tracked?: number | null;
		cameras_active?: number | null;
		last_heartbeat?: string | number | null;
	};
	updated_at?: string | null;
}

/**
 * Fetch the current drive-server state. The drive server writes
 * `api/state.json` under the published data directory every few seconds.
 */
export async function fetchState(): Promise<StateJson> {
	const config = await loadRuntimeConfig();
	const url = `${buildAssetUrl(config.dataBaseUrl, '/api/state.json')}?_t=${Date.now()}`;
	const response = await fetch(url, { cache: 'no-store' });

	if (!response.ok) {
		throw new Error(`Failed to fetch state: ${response.status}`);
	}

	return response.json() as Promise<StateJson>;
}

/**
 * Road polyline data for the map overlay, exported by the drive server from
 * the active CARLA map. Each polyline is an array of [lon, lat] pairs.
 */
export interface MapDataResponse {
	geo_ref: {
		map_name: string;
		origin_lat: number;
		origin_lon: number;
		origin_alt: number;
		proj_string?: string;
	};
	road_network: number[][][];
}

export async function fetchMapDataFull(): Promise<MapDataResponse> {
	const config = await loadRuntimeConfig();
	const response = await fetch(buildAssetUrl(config.dataBaseUrl, '/api/map-data.json'));

	if (!response.ok) {
		throw new Error(`Failed to fetch map data: ${response.status}`);
	}

	return (await response.json()) as MapDataResponse;
}

async function readErrorDetail(response: Response): Promise<string> {
	let detail = `${response.status}`;
	try {
		const body = (await response.json()) as { detail?: string; error?: string };
		detail = body.detail || body.error || detail;
	} catch {
		// Keep the HTTP status fallback.
	}
	return detail;
}

export interface ArchiveSegment {
	start: string;
	duration: number;
	url?: string;
}

export function archiveListUrl(
	baseUrl: string,
	cameraId: string,
	start: string,
	end: string
): string {
	const url = new URL(`${baseUrl.replace(/\/+$/, '')}/list`, pageOrigin());
	url.searchParams.set('path', cameraId);
	url.searchParams.set('start', start);
	url.searchParams.set('end', end);
	return url.toString();
}

export function archiveClipUrl(
	baseUrl: string,
	cameraId: string,
	start: string,
	durationSeconds: number
): string {
	const url = new URL(`${baseUrl.replace(/\/+$/, '')}/get`, pageOrigin());
	url.searchParams.set('path', cameraId);
	url.searchParams.set('start', start);
	url.searchParams.set('duration', String(durationSeconds));
	url.searchParams.set('format', 'mp4');
	return url.toString();
}

/** Base for resolving root-relative config URLs; tests run without a window. */
function pageOrigin(): string {
	return typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
}

export async function listArchiveSegments(
	cameraId: string,
	start: string,
	end: string
): Promise<ArchiveSegment[]> {
	const config = await loadRuntimeConfig();
	if (!config.archiveVideoBaseUrl) return [];

	const response = await fetch(
		archiveListUrl(config.archiveVideoBaseUrl, cameraId, start, end),
		{ cache: 'no-store' }
	);
	if (response.status === 404) return [];
	if (!response.ok) {
		throw new Error(`Failed to list archive recordings: ${await readErrorDetail(response)}`);
	}

	const segments = (await response.json()) as ArchiveSegment[];
	return segments
		.filter(
			(segment) =>
				typeof segment.start === 'string' &&
				Number.isFinite(Date.parse(segment.start)) &&
				Number.isFinite(segment.duration) &&
				segment.duration > 0
		)
		.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
}

// ── Twin server detection history ──

async function fetchDetectionsJson<T>(
	route: '/coverage' | '/history' | '/objects',
	params: Record<string, string>,
	label: string
): Promise<T> {
	const config = await loadRuntimeConfig();
	const url = new URL(`${config.detectionsBaseUrl}${route}`, pageOrigin());
	for (const [key, value] of Object.entries(params)) {
		url.searchParams.set(key, value);
	}
	const response = await fetch(url, {
		headers: { accept: 'application/json' },
		cache: 'no-store'
	});
	if (!response.ok) {
		throw new Error(`Failed to fetch ${label}: ${await readErrorDetail(response)}`);
	}
	return (await response.json()) as T;
}

export function fetchDetectionCoverage(options: {
	start: string;
	end: string;
	bucketSeconds: number;
}): Promise<DetectionCoverage> {
	return fetchDetectionsJson<DetectionCoverage>(
		'/coverage',
		{ start: options.start, end: options.end, bucket: String(options.bucketSeconds) },
		'detection coverage'
	);
}

export async function fetchDetectionObjects(options: {
	start: string;
	end: string;
	limit?: number;
}): Promise<DetectionObject[]> {
	const page = await fetchDetectionsJson<{ items: DetectionObject[] }>(
		'/objects',
		{ start: options.start, end: options.end, limit: String(options.limit ?? 200) },
		'detection objects'
	);
	return page.items;
}

export function fetchDetectionHistory(options: {
	start: string;
	end: string;
	limit?: number;
}): Promise<DetectionHistoryPage> {
	return fetchDetectionsJson<DetectionHistoryPage>(
		'/history',
		{ start: options.start, end: options.end, limit: String(options.limit ?? 50) },
		'detections'
	);
}

// ── Demo videos (nginx JSON autoindex of the published demo-videos directory) ──

interface AutoindexEntry {
	name: string;
	type: 'file' | 'directory';
	mtime?: string;
	size?: number;
}

const DEMO_VIDEO_EXTENSIONS = ['.mp4', '.webm', '.mov', '.m4v'];

export function demoVideoTitle(fileName: string): string {
	const stem = fileName.replace(/\.[^.]+$/, '') || fileName;
	const words = stem.replace(/[_-]+/g, ' ').trim();
	return words || fileName;
}

export async function fetchDemoVideos(): Promise<DemoVideo[]> {
	const config = await loadRuntimeConfig();
	const directoryUrl = `${buildAssetUrl(config.dataBaseUrl, '/demo-videos')}/`;
	const response = await fetch(directoryUrl, {
		headers: { accept: 'application/json' },
		cache: 'no-store'
	});

	if (!response.ok) {
		throw new Error(`Failed to fetch demo videos: ${response.status}`);
	}

	const entries = (await response.json()) as AutoindexEntry[];
	return entries
		.filter(
			(entry) =>
				entry.type === 'file' &&
				DEMO_VIDEO_EXTENSIONS.some((extension) => entry.name.toLowerCase().endsWith(extension))
		)
		.map((entry) => ({
			fileName: entry.name,
			title: demoVideoTitle(entry.name),
			url: `${directoryUrl}${encodeURIComponent(entry.name)}`,
			sizeBytes: entry.size ?? 0,
			lastModified: entry.mtime ? new Date(entry.mtime).toISOString() : null
		}))
		.sort((a, b) => (b.lastModified ?? '').localeCompare(a.lastModified ?? ''));
}
