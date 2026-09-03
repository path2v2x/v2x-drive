/**
 * Runtime configuration loaded from `/config.json` next to the built site.
 *
 * Every URL may be relative: the site is served from the same nginx host as
 * the drive server, MediaMTX and the twin server, so production config uses
 * root-relative paths and the browser origin supplies the host.
 */
export interface RuntimeConfig {
	/** Drive server WebSocket; relative paths resolve against the page origin. */
	wsUrl: string;
	/** Static data published by the drive server (`api/state.json`, `api/map-data.json`, demo videos). */
	dataBaseUrl: string;
	/** Twin server detection history (`/coverage`, `/history`, `/objects`). */
	detectionsBaseUrl: string;
	videoCameraIds: string[];
	/** Live camera HLS playlist per camera; `{camera_id}` placeholder. Empty disables live video. */
	liveVideoUrlTemplate: string;
	/** MediaMTX playback server base; empty disables archive playback. */
	archiveVideoBaseUrl: string;
}

const DEFAULT_CONFIG: RuntimeConfig = {
	wsUrl: '/ws',
	dataBaseUrl: '/data',
	detectionsBaseUrl: '/detections',
	videoCameraIds: ['ch1', 'ch2', 'ch3', 'ch4'],
	liveVideoUrlTemplate: '',
	archiveVideoBaseUrl: ''
};

let configPromise: Promise<RuntimeConfig> | null = null;

function trimTrailingSlash(value: string | undefined, fallback: string): string {
	return (value || fallback).trim().replace(/\/+$/, '');
}

function normalizeConfig(config: Partial<RuntimeConfig>): RuntimeConfig {
	return {
		wsUrl: (config.wsUrl || DEFAULT_CONFIG.wsUrl).trim(),
		dataBaseUrl: trimTrailingSlash(config.dataBaseUrl, DEFAULT_CONFIG.dataBaseUrl),
		detectionsBaseUrl: trimTrailingSlash(config.detectionsBaseUrl, DEFAULT_CONFIG.detectionsBaseUrl),
		videoCameraIds: config.videoCameraIds || DEFAULT_CONFIG.videoCameraIds,
		liveVideoUrlTemplate: config.liveVideoUrlTemplate || DEFAULT_CONFIG.liveVideoUrlTemplate,
		archiveVideoBaseUrl: trimTrailingSlash(config.archiveVideoBaseUrl, DEFAULT_CONFIG.archiveVideoBaseUrl)
	};
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
	if (!configPromise) {
		const configUrl = `/config.json?v=${Date.now()}`;
		configPromise = fetch(configUrl, { cache: 'no-store' })
			.then(async (response) => {
				if (!response.ok) {
					return DEFAULT_CONFIG;
				}
				return normalizeConfig((await response.json()) as Partial<RuntimeConfig>);
			})
			.catch(() => DEFAULT_CONFIG);
	}

	return configPromise;
}

/** Clear the memoized config so an explicit refresh can reload config.json. */
export function resetRuntimeConfigCache(): void {
	configPromise = null;
}

export function resolveLiveVideoUrl(template: string, cameraId: string): string {
	return template.trim().replace('{camera_id}', encodeURIComponent(cameraId));
}

export function buildAssetUrl(baseUrl: string, path: string): string {
	const normalizedPath = path.startsWith('/') ? path : `/${path}`;
	return `${baseUrl.replace(/\/+$/, '')}${normalizedPath}`;
}

/**
 * Resolve the drive WebSocket URL. Root-relative paths use the page origin
 * (`wss:` on https pages); `http(s)://` values are rewritten to `ws(s)://`.
 */
export function resolveWsUrl(
	wsUrl: string,
	pageLocation: Pick<Location, 'protocol' | 'host'> | undefined = typeof window === 'undefined'
		? undefined
		: window.location
): string {
	const trimmed = wsUrl.trim();
	if (!trimmed) return '';
	if (trimmed.startsWith('/')) {
		if (!pageLocation) return '';
		const scheme = pageLocation.protocol === 'https:' ? 'wss' : 'ws';
		return `${scheme}://${pageLocation.host}${trimmed}`;
	}
	return trimmed.replace(/^https:\/\//, 'wss://').replace(/^http:\/\//, 'ws://').replace(/\/$/, '');
}
