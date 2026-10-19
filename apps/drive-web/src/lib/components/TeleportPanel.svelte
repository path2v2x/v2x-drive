<script lang="ts">
	import { onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import { MAP_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from '$lib/constants';
	import { carlaToGps, carlaYawToBearing } from '$lib/geo';
	import {
		driveConnected,
		resetTeleportStatus,
		sessionState,
		telemetry,
		teleportStatus,
		teleportVehicle,
		TELEPORT_COORD_ABS_LIMIT_M,
		TELEPORT_MAX_ABS_YAW_DEG,
		TELEPORT_MAX_Z_M,
		TELEPORT_MIN_Z_M
	} from '$lib/stores/driveSocket';

	interface Props {
		onClose: () => void;
		roadLines?: number[][][];
		originLat?: number | null;
		originLon?: number | null;
	}

	let { onClose, roadLines = [], originLat = null, originLon = null }: Props = $props();

	// Map mode needs the geo reference to convert clicks → CARLA coordinates.
	const hasGeoRef = originLat !== null && originLon !== null;
	let mode = $state<'map' | 'manual'>(hasGeoRef ? 'map' : 'manual');

	let x = $state<number | undefined>(undefined);
	let y = $state<number | undefined>(undefined);
	let z = $state<number | undefined>(undefined);
	let yaw = $state<number | undefined>(undefined);

	let pending = $derived($teleportStatus.state === 'pending');
	let sessionActive = $derived($driveConnected && $sessionState === 'driving');
	let validCoordinates = $derived(
		typeof x === 'number'
			&& Number.isFinite(x)
			&& Math.abs(x) <= TELEPORT_COORD_ABS_LIMIT_M
			&& typeof y === 'number'
			&& Number.isFinite(y)
			&& Math.abs(y) <= TELEPORT_COORD_ABS_LIMIT_M
	);

	function optionalNumber(value: number | undefined): number | undefined {
		return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
	}

	function apply(event: SubmitEvent) {
		event.preventDefault();
		if (x === undefined || y === undefined) return;
		teleportVehicle(x, y, optionalNumber(z), optionalNumber(yaw));
	}

	function useCurrent() {
		const current = $telemetry.pos;
		x = Math.round(current[0] * 10) / 10;
		y = Math.round(current[1] * 10) / 10;
		z = Math.round(current[2] * 10) / 10;
		resetTeleportStatus();
	}

	function clearSettledStatus() {
		if (!pending) resetTeleportStatus();
	}

	// ── Map mode ────────────────────────────────────────────────────────
	// Inverse of carlaToGps (see $lib/geo).
	function gpsToCarla(lon: number, lat: number): [number, number] {
		const METERS_PER_DEGREE = 111320;
		const cy = (originLat! - lat) * METERS_PER_DEGREE;
		const cx = (lon - originLon!) * METERS_PER_DEGREE * Math.cos((originLat! * Math.PI) / 180);
		return [cx, cy];
	}

	let mapContainer = $state<HTMLDivElement | null>(null);
	let map: maplibregl.Map | null = null;
	let carMarker: maplibregl.Marker | null = null;
	let targetMarker: maplibregl.Marker | null = null;
	let mapReady = $state(false);
	let mapClickNote = $state('');
	let frameCount = 0;

	// The map turns with the car (heading-up, like the mini-map), so the arrow, which
	// always points up, shows the real direction of travel. The turn happens about the
	// car rather than the map centre, so a pan the user made is kept, and it pauses
	// while the user drags.
	let userDragging = false;
	let lastBearing: number | null = null;

	function rotateToHeading(yawDeg: number, carLngLat: [number, number]) {
		if (!map || userDragging) return;
		const bearing = carlaYawToBearing(yawDeg);
		if (lastBearing !== null && Math.abs(((bearing - lastBearing + 540) % 360) - 180) < 0.5) return;
		lastBearing = bearing;
		const before = map.project(carLngLat);
		map.setBearing(bearing);
		const after = map.project(carLngLat);
		map.panBy([after.x - before.x, after.y - before.y], { duration: 0 });
	}

	function centerOnCar() {
		if (!map || !hasGeoRef) return;
		const t = $telemetry;
		if (t?.pos && (t.pos[0] !== 0 || t.pos[1] !== 0)) {
			const [lon, lat] = carlaToGps(t.pos[0], t.pos[1], originLat!, originLon!);
			lastBearing = carlaYawToBearing(t.rot[1]);
			map.jumpTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), DEFAULT_ZOOM + 1), bearing: lastBearing });
		}
	}

	function handleMapClick(e: maplibregl.MapMouseEvent) {
		if (!hasGeoRef || !sessionActive || pending) return;
		const [cx, cy] = gpsToCarla(e.lngLat.lng, e.lngLat.lat);
		if (Math.abs(cx) > TELEPORT_COORD_ABS_LIMIT_M || Math.abs(cy) > TELEPORT_COORD_ABS_LIMIT_M) {
			mapClickNote = 'That point is outside the teleportable area.';
			return;
		}
		mapClickNote = '';

		// Drop / move the emerald target marker
		if (!targetMarker && map) {
			const tel = document.createElement('div');
			tel.innerHTML = `<svg width="22" height="22" viewBox="0 0 24 24" style="filter: drop-shadow(0 0 5px rgba(16,185,129,0.8));">
				<circle cx="12" cy="12" r="6" fill="none" stroke="#10b981" stroke-width="2.5"/>
				<circle cx="12" cy="12" r="1.8" fill="#10b981"/>
			</svg>`;
			tel.style.width = '22px';
			tel.style.height = '22px';
			targetMarker = new maplibregl.Marker({ element: tel }).setLngLat(e.lngLat).addTo(map);
		} else {
			targetMarker?.setLngLat(e.lngLat);
		}

		// Sync the manual fields and fire (z omitted → snap to road)
		x = Math.round(cx * 10) / 10;
		y = Math.round(cy * 10) / 10;
		resetTeleportStatus();
		teleportVehicle(cx, cy);
	}

	function initMap(container: HTMLDivElement) {
		map = new maplibregl.Map({
			container,
			style: MAP_STYLE_URL,
			center: [originLon ?? MAP_CENTER.lon, originLat ?? MAP_CENTER.lat],
			zoom: DEFAULT_ZOOM + 1,
			attributionControl: false,
			dragRotate: false,
			keyboard: false,
		});
		map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

		map.on('load', () => {
			if (!map) return;
			mapReady = true;

			if (roadLines.length > 0) {
				map.addSource('tp-roads', {
					type: 'geojson',
					data: {
						type: 'FeatureCollection',
						features: roadLines.map((coords) => ({
							type: 'Feature' as const,
							geometry: { type: 'LineString' as const, coordinates: coords },
							properties: {},
						})),
					},
				});
				map.addLayer({
					id: 'tp-roads-layer',
					type: 'line',
					source: 'tp-roads',
					paint: { 'line-color': '#6b7280', 'line-width': 1.5, 'line-opacity': 0.6 },
				});
			}

			// Live car marker (cyan arrow, same styling as the minimap)
			const el = document.createElement('div');
			el.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" style="filter: drop-shadow(0 0 4px rgba(34,211,238,0.6));">
				<polygon points="12,2 4,20 12,16 20,20" fill="#22d3ee" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round"/>
			</svg>`;
			el.style.width = '20px';
			el.style.height = '20px';
			carMarker = new maplibregl.Marker({ element: el, rotationAlignment: 'viewport' })
				.setLngLat([originLon ?? MAP_CENTER.lon, originLat ?? MAP_CENTER.lat])
				.addTo(map);

			centerOnCar();
		});

		map.on('click', handleMapClick);
		map.on('dragstart', () => {
			userDragging = true;
		});
		map.on('dragend', () => {
			userDragging = false;
		});
	}

	function teardownMap() {
		carMarker?.remove();
		targetMarker?.remove();
		map?.remove();
		carMarker = null;
		targetMarker = null;
		map = null;
		mapReady = false;
		lastBearing = null;
	}

	// Create the map when its container mounts (Map tab active) and drop it when the
	// tab goes away: the container is destroyed with the tab, so the old map instance
	// would otherwise come back blank on the next switch to Map.
	$effect(() => {
		if (mode === 'map' && mapContainer && !map) {
			initMap(mapContainer);
		} else if (mode !== 'map' && map) {
			teardownMap();
		}
	});

	// Live car position on the panel map (throttled to ~5fps).
	$effect(() => {
		const t = $telemetry;
		if (!map || !mapReady || !carMarker || !hasGeoRef) return;
		frameCount++;
		if (frameCount % 4 !== 0) return;
		const [lon, lat] = carlaToGps(t.pos[0], t.pos[1], originLat!, originLon!);
		carMarker.setLngLat([lon, lat]);
		rotateToHeading(t.rot[1], [lon, lat]);
	});

	onDestroy(() => {
		teardownMap();
	});
</script>

<div
	class="absolute bottom-16 right-2 z-30 flex {mode === 'map' ? 'w-96' : 'w-72'} flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-900/95 text-gray-200 shadow-xl backdrop-blur-md pointer-events-auto"
	data-testid="teleport-panel"
>
	<div class="flex items-center justify-between border-b border-gray-700 px-3 py-2">
		<span class="text-sm font-semibold tracking-wide text-emerald-400">Teleport</span>
		<div class="flex items-center gap-1">
			{#if hasGeoRef}
				<button
					type="button"
					onclick={() => (mode = 'map')}
					aria-pressed={mode === 'map'}
					data-testid="teleport-mode-map"
					class="cursor-pointer rounded px-2 py-0.5 text-[11px] font-medium {mode === 'map'
						? 'bg-emerald-600 text-white'
						: 'text-gray-400 hover:text-white'}">Map</button
				>
			{/if}
			<button
				type="button"
				onclick={() => (mode = 'manual')}
				aria-pressed={mode === 'manual'}
				data-testid="teleport-mode-manual"
				class="cursor-pointer rounded px-2 py-0.5 text-[11px] font-medium {mode === 'manual'
					? 'bg-emerald-600 text-white'
					: 'text-gray-400 hover:text-white'}">Manual</button
			>
			<button
				type="button"
				onclick={onClose}
				class="ml-1 cursor-pointer px-1 text-xl leading-none text-gray-400 hover:text-white"
				aria-label="Close teleport panel"
			>×</button>
		</div>
	</div>

	{#if mode === 'map'}
		<div class="flex flex-col" data-testid="teleport-map-mode">
			<div bind:this={mapContainer} class="h-64 w-full"></div>
			<div class="flex items-center justify-between gap-2 border-t border-gray-800 px-3 py-2 text-xs">
				<span class="text-gray-400">
					{sessionActive ? 'Click the map to teleport there. Up is the way you face.' : 'Start a drive session first.'}
				</span>
				<button
					type="button"
					onclick={centerOnCar}
					class="cursor-pointer whitespace-nowrap rounded bg-gray-700 px-2 py-1 text-white hover:bg-gray-600"
					title="Center the map on the car">Find car</button
				>
			</div>
			{#if mapClickNote}
				<p class="px-3 pb-2 text-xs text-amber-300" role="status">{mapClickNote}</p>
			{:else if $teleportStatus.message}
				<p
					class="px-3 pb-2 font-mono text-xs {$teleportStatus.state === 'error' ? 'text-red-300' : $teleportStatus.state === 'succeeded' ? 'text-emerald-400' : 'text-amber-300'}"
					role={$teleportStatus.state === 'error' ? 'alert' : 'status'}
					aria-live="polite"
					data-testid="teleport-status"
				>
					{$teleportStatus.message}
				</p>
			{/if}
		</div>
	{:else}
		<form class="flex flex-col gap-2 p-3 text-xs" onsubmit={apply}>
			<p class="leading-relaxed text-gray-400">
				Move this session's ego car to a world coordinate. Leave
				<span class="font-mono text-gray-300">Z</span> blank to snap to the nearest road.
			</p>

			<label class="flex items-center gap-2">
				<span class="w-9 font-mono text-gray-400">X</span>
				<input
					type="number"
					bind:value={x}
					oninput={clearSettledStatus}
					min={-TELEPORT_COORD_ABS_LIMIT_M}
					max={TELEPORT_COORD_ABS_LIMIT_M}
					step="any"
					required
					disabled={pending}
					aria-label="Teleport X coordinate"
					class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
				/>
			</label>
			<label class="flex items-center gap-2">
				<span class="w-9 font-mono text-gray-400">Y</span>
				<input
					type="number"
					bind:value={y}
					oninput={clearSettledStatus}
					min={-TELEPORT_COORD_ABS_LIMIT_M}
					max={TELEPORT_COORD_ABS_LIMIT_M}
					step="any"
					required
					disabled={pending}
					aria-label="Teleport Y coordinate"
					class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
				/>
			</label>
			<label class="flex items-center gap-2">
				<span class="w-9 font-mono text-gray-400">Z</span>
				<input
					type="number"
					bind:value={z}
					oninput={clearSettledStatus}
					min={TELEPORT_MIN_Z_M}
					max={TELEPORT_MAX_Z_M}
					step="any"
					placeholder="auto (road)"
					disabled={pending}
					aria-label="Teleport Z coordinate"
					class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
				/>
			</label>
			<label class="flex items-center gap-2">
				<span class="w-9 font-mono text-gray-400">Yaw°</span>
				<input
					type="number"
					bind:value={yaw}
					oninput={clearSettledStatus}
					min={-TELEPORT_MAX_ABS_YAW_DEG}
					max={TELEPORT_MAX_ABS_YAW_DEG}
					step="any"
					placeholder="keep"
					disabled={pending}
					aria-label="Teleport yaw"
					class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
				/>
			</label>

			<div class="mt-1 flex gap-2">
				<button
					type="button"
					onclick={useCurrent}
					disabled={pending}
					class="flex-1 cursor-pointer rounded bg-gray-700 px-2 py-1.5 text-white hover:bg-gray-600 disabled:cursor-wait disabled:opacity-60"
					title="Fill with the car's current position"
				>Use current</button>
				<button
					type="submit"
					disabled={!sessionActive || !validCoordinates || pending}
					class="flex-1 cursor-pointer rounded bg-emerald-600 px-2 py-1.5 font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
				> {pending ? 'Waiting…' : 'Teleport'} </button>
			</div>

			{#if !sessionActive}
				<p class="mt-0.5 text-amber-300" data-testid="teleport-session-warning">
					Start an active drive session before teleporting.
				</p>
			{:else if $teleportStatus.message}
				<p
					class="mt-0.5 font-mono {$teleportStatus.state === 'error' ? 'text-red-300' : $teleportStatus.state === 'succeeded' ? 'text-emerald-400' : 'text-amber-300'}"
					role={$teleportStatus.state === 'error' ? 'alert' : 'status'}
					aria-live="polite"
					data-testid="teleport-status"
				>
					{$teleportStatus.message}
				</p>
			{/if}
		</form>
	{/if}
</div>
