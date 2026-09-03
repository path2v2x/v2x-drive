export const MAP_CENTER = { lat: 37.915, lon: -122.335 };

export const DEFAULT_ZOOM = 16;

export const OBJECT_COLORS: Record<string, string> = {
	traffic_cone: '#FF8C00',
	vehicle: '#0078FF',
	walker: '#00C850',
	default: '#FF5050'
};

export const FRESHNESS_THRESHOLDS = {
	fresh: 10_000, // < 10 seconds
	stale: 30_000 // < 30 seconds; beyond this is "old"
}; // ms

export const POLL_INTERVAL = 3000; // ms - how often to poll state.json

export const MAP_STYLE_URL =
	'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';

export const SNAPSHOT_PLACEHOLDER =
	'data:image/svg+xml,' +
	encodeURIComponent(
		'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">' +
			'<rect fill="#1f2937" width="320" height="240"/>' +
			'<text fill="#6b7280" font-family="system-ui" font-size="14" text-anchor="middle" x="160" y="125">No snapshot available</text>' +
			'</svg>'
	);

// ── Drive Mode Constants ──

export const GAMEPAD_DEADZONE = 0.005;

export const GAMEPAD_POLL_RATE = 60;

// Logitech G923 (046d:c266) — 10 axes, 25 buttons
// Axis 0 = steering, Axis 1 = brake, Axis 2 = gas, Axis 3 = clutch
// Pedals rest at +1.0 and travel toward -1.0 when pressed; hardcoded so input
// works at spawn without waiting on a sweep-detection window.
export const DEFAULT_CALIBRATION = {
	steerAxis: 0,
	gasAxis: 2,
	brakeAxis: 1,
	steerInverted: false,
	gasInverted: false,
	brakeInverted: false,
	gasRest: 1.0,
	brakeRest: 1.0,
};

export const CAMERA_VIEWS = [
	{ id: 'chase', label: 'Chase', key: '1' },
	{ id: 'hood', label: 'Hood', key: '2' },
	{ id: 'bird', label: "Bird's Eye", key: '3' },
	{ id: 'free', label: 'Free Look', key: '4' },
] as const;
