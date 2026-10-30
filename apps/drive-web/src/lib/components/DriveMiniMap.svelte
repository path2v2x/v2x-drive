<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import { MAP_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from '$lib/constants';
	import { dynamicActors, telemetry } from '$lib/stores/driveSocket';
	import { v2xZones } from '$lib/stores/v2xZones';
	import { carlaToGps } from '$lib/stores/v2xZones';
	import { carlaYawToBearing } from '$lib/geo';
	import { shouldDrawZone } from '$lib/zoneRules';
	import { buildActorGeofencePolygon, DYNAMIC_GEOFENCE_COLOR } from '$lib/actorGeofenceRules';
	import type { DynamicActor, V2xZone } from '$lib/types';

	interface Props {
		roadLines: number[][][];
		originLat: number;
		originLon: number;
		fullPanel?: boolean;
		/** where the floating mini-map sits over the camera (the driving HUD takes the top-left) */
		corner?: 'top-left' | 'bottom-right';
	}

	let { roadLines, originLat, originLon, fullPanel = false, corner = 'top-left' }: Props = $props();

	let mapContainer: HTMLDivElement;
	let map: maplibregl.Map | null = null;
	let carMarker: maplibregl.Marker | null = null;
	let mapReady = $state(false);
	let expanded = $state(false);
	let drawableZoneCount = $derived($v2xZones.filter(shouldDrawZone).length);
	let dynamicActorGeofenceSignature = '';

	onMount(() => {
		map = new maplibregl.Map({
			container: mapContainer,
			style: MAP_STYLE_URL,
			center: [originLon || MAP_CENTER.lon, originLat || MAP_CENTER.lat],
			zoom: fullPanel ? DEFAULT_ZOOM : DEFAULT_ZOOM + 1,
			attributionControl: false,
			dragPan: fullPanel,
			dragRotate: false,
			keyboard: false,
			doubleClickZoom: fullPanel,
			touchZoomRotate: fullPanel,
			scrollZoom: true,
		});

		if (fullPanel) {
			map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
		}

		map.on('load', () => {
			if (!map) return;
			mapReady = true;

			// Road network
			if (roadLines.length > 0) {
				map.addSource('roads', {
					type: 'geojson',
					data: buildRoadGeoJSON(roadLines),
				});
				map.addLayer({
					id: 'roads-layer',
					type: 'line',
					source: 'roads',
					paint: {
						'line-color': '#6b7280',
						'line-width': 1.5,
						'line-opacity': 0.5,
					},
				});
			}

			// V2X zones
			map.addSource('v2x-zones', {
				type: 'geojson',
				data: buildZonesGeoJSON($v2xZones),
			});
			map.addLayer({
				id: 'v2x-zones-fill',
				type: 'fill',
				source: 'v2x-zones',
				paint: {
					'fill-color': ['get', 'color'],
					'fill-opacity': 0.3,
				},
			});
			map.addLayer({
				id: 'v2x-zones-outline',
				type: 'line',
				source: 'v2x-zones',
				paint: {
					'line-color': ['get', 'color'],
					'line-width': 1.5,
					'line-opacity': 0.7,
				},
			});

			// Moving geofences follow dynamic autopilot actors.
			const initialDynamicActors = $dynamicActors;
			dynamicActorGeofenceSignature = getDynamicActorGeofenceSignature(initialDynamicActors);
			map.addSource('dynamic-actor-geofences', {
				type: 'geojson',
				data: buildDynamicActorGeofenceGeoJSON(initialDynamicActors),
			});
			map.addLayer({
				id: 'dynamic-actor-geofences-fill',
				type: 'fill',
				source: 'dynamic-actor-geofences',
				paint: {
					'fill-color': ['get', 'color'],
					'fill-opacity': 0.18,
				},
			});
			map.addLayer({
				id: 'dynamic-actor-geofences-outline',
				type: 'line',
				source: 'dynamic-actor-geofences',
				paint: {
					'line-color': ['get', 'color'],
					'line-width': 2,
					'line-opacity': 0.85,
				},
			});

			// Nearby actors layer (traffic vehicles + other actors)
			map.addSource('nearby-actors', {
				type: 'geojson',
				data: { type: 'FeatureCollection', features: [] },
			});
			map.addLayer({
				id: 'nearby-actors-layer',
				type: 'circle',
				source: 'nearby-actors',
				paint: {
					'circle-radius': 4,
					'circle-color': [
						'match', ['get', 'type'],
						'dynamic', '#ef4444',  // red for moving geofence actors
						'traffic', '#f59e0b',  // amber for NPC traffic
						'erv', '#f43f5e',      // rose for fleet emergency vehicles
						'civilian', '#38bdf8', // sky for fleet civilians
						'#94a3b8',             // gray for other vehicles
					],
					'circle-stroke-width': 1,
					'circle-stroke-color': '#1f2937',
					'circle-opacity': 0.85,
				},
			});

			// Names above the actor dots (role names; the server leaves its own traffic unnamed)
			map.addLayer({
				id: 'nearby-actors-labels',
				type: 'symbol',
				source: 'nearby-actors',
				filter: ['!=', ['get', 'name'], ''],
				layout: {
					'text-field': ['get', 'name'],
					'text-font': ['Open Sans Bold'],
					'text-size': 10,
					'text-anchor': 'bottom',
					'text-offset': [0, -0.8],
					'text-allow-overlap': true,
					'text-ignore-placement': true,
					'text-rotation-alignment': 'viewport',
					'text-pitch-alignment': 'viewport',
				},
				paint: {
					'text-color': '#f8fafc',
					'text-halo-color': '#0f172a',
					'text-halo-width': 1.4,
				},
			});

			// Car marker — directional arrow that rotates with heading
			const el = document.createElement('div');
			el.className = 'car-marker';
			el.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" style="filter: drop-shadow(0 0 4px rgba(34, 211, 238, 0.6));">
				<polygon points="12,2 4,20 12,16 20,20" fill="#22d3ee" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round"/>
			</svg>`;
			el.style.width = '24px';
			el.style.height = '24px';

			carMarker = new maplibregl.Marker({ element: el, rotationAlignment: 'viewport' })
				.setLngLat([originLon || MAP_CENTER.lon, originLat || MAP_CENTER.lat])
				.addTo(map);
		});
	});

	function buildActorsGeoJSON(actors: { id: number; pos: [number, number]; type: string; name?: string }[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: actors.map((a) => {
				const [lon, lat] = carlaToGps(a.pos[0], a.pos[1], originLat, originLon);
				return {
					type: 'Feature' as const,
					geometry: { type: 'Point' as const, coordinates: [lon, lat] },
					properties: { id: a.id, type: a.type, name: a.name ?? '' },
				};
			}),
		};
	}

	function getDynamicActorGeofenceSignature(actors: DynamicActor[]): string {
		return actors
			.map((actor) => [
				actor.actor_id,
				actor.pos?.[0],
				actor.pos?.[1],
				actor.pos?.[2],
				actor.yaw,
				actor.geofence_radius,
				actor.name,
			].join(':'))
			.sort()
			.join('|');
	}

	function buildDynamicActorGeofenceGeoJSON(actors: DynamicActor[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: actors.flatMap((actor) => {
				const polygon = buildActorGeofencePolygon(actor, originLat, originLon);
				if (polygon.length < 4) return [];

				return [{
					type: 'Feature' as const,
					geometry: {
						type: 'Polygon' as const,
						coordinates: [polygon],
					},
					properties: {
						actor_id: actor.actor_id,
						name: actor.name,
						color: DYNAMIC_GEOFENCE_COLOR,
					},
				}];
			}),
		};
	}

	// Update car position from telemetry
	let frameCount = 0;
	$effect(() => {
		const t = $telemetry;
		if (!map || !mapReady || !carMarker) return;

		// Throttle to ~5fps (every 4th telemetry update at 20fps)
		frameCount++;
		if (frameCount % 4 !== 0) return;

		const [lon, lat] = carlaToGps(t.pos[0], t.pos[1], originLat, originLon);
		carMarker.setLngLat([lon, lat]);
		// Google/Apple Maps nav style: arrow always points UP (direction of travel),
		// map rotates underneath so "up" = forward.
		// MapLibre bearing = clockwise from north. We set bearing to the car's
		// heading so north rotates and the car's forward is always screen-up.
		const heading = carlaYawToBearing(t.rot[1]);
		carMarker.setRotation(0);
		map.jumpTo({ center: [lon, lat], bearing: heading });

		// Update nearby actors
		const actorsSource = map.getSource('nearby-actors') as maplibregl.GeoJSONSource | undefined;
		if (actorsSource && t.nearby_actors) {
			actorsSource.setData(buildActorsGeoJSON(t.nearby_actors));
		}
	});

	// Update zone overlays when zones change
	$effect(() => {
		const zones = $v2xZones;
		if (!map || !mapReady) return;
		const source = map.getSource('v2x-zones') as maplibregl.GeoJSONSource | undefined;
		if (source) {
			source.setData(buildZonesGeoJSON(zones));
		}
	});

	// Update moving geofence overlays on every dynamic actor store change.
	$effect(() => {
		const actors = $dynamicActors;
		if (!map || !mapReady) return;
		const signature = getDynamicActorGeofenceSignature(actors);
		if (signature === dynamicActorGeofenceSignature) return;
		const source = map.getSource('dynamic-actor-geofences') as maplibregl.GeoJSONSource | undefined;
		if (source) {
			source.setData(buildDynamicActorGeofenceGeoJSON(actors));
			dynamicActorGeofenceSignature = signature;
		}
	});

	function buildRoadGeoJSON(lines: number[][][]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: lines.map((coords) => ({
				type: 'Feature' as const,
				geometry: { type: 'LineString' as const, coordinates: coords },
				properties: {},
			})),
		};
	}

	function buildZonesGeoJSON(zones: V2xZone[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: zones
				.filter((z) => shouldDrawZone(z) && z.polygon.length >= 3)
				.map((z) => ({
					type: 'Feature' as const,
					geometry: {
						type: 'Polygon' as const,
						coordinates: [z.polygon],
					},
					properties: {
						color: z.color,
						name: z.name,
						zone_kind: z.zone_kind,
					},
				})),
		};
	}

	onDestroy(() => {
		if (carMarker) carMarker.remove();
		if (map) map.remove();
		map = null;
		carMarker = null;
	});
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class={fullPanel
		? 'relative h-full w-full'
		: `absolute ${corner === 'bottom-right' ? 'right-3 bottom-3' : 'left-3 top-3'} z-30 overflow-hidden rounded-lg border border-gray-700/60 bg-gray-900/80 shadow-xl backdrop-blur-sm transition-all duration-200`}
	style={fullPanel ? '' : `width: ${expanded ? 400 : 220}px; height: ${expanded ? 300 : 160}px;`}
	onclick={() => { if (!fullPanel) { expanded = !expanded; setTimeout(() => map?.resize(), 210); } }}
>
	<div bind:this={mapContainer} class="h-full w-full"></div>

	<!-- Zoom controls -->
	<div class="absolute top-1 right-1 z-10 flex flex-col gap-0.5 pointer-events-auto"
		onclick={(e) => e.stopPropagation()}>
		<button
			class="w-6 h-6 rounded bg-gray-800/90 border border-gray-700/60 text-gray-300 hover:text-white hover:bg-gray-700 text-xs font-bold flex items-center justify-center transition-colors"
			onclick={(e) => { e.stopPropagation(); if (map) map.zoomIn(); }}
		>+</button>
		<button
			class="w-6 h-6 rounded bg-gray-800/90 border border-gray-700/60 text-gray-300 hover:text-white hover:bg-gray-700 text-xs font-bold flex items-center justify-center transition-colors"
			onclick={(e) => { e.stopPropagation(); if (map) map.zoomOut(); }}
		>-</button>
	</div>

	<!-- Zone count badge -->
	{#if drawableZoneCount > 0 || $dynamicActors.length > 0}
		<div class="absolute bottom-1 right-1 rounded bg-gray-900/80 px-1.5 py-0.5 text-[9px] font-medium text-gray-400">
			{drawableZoneCount} static / {$dynamicActors.length} moving
		</div>
	{/if}
</div>
