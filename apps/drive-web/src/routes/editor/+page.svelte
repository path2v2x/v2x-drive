<script lang="ts">
	// Editor mode: a free camera over the shared CARLA world, an outliner of every
	// actor in it, and details of the selected one. Read-only: nothing here moves
	// or deletes anything but the camera.
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import CameraViewComponent from '$lib/components/CameraView.svelte';
	import V2xPerspectivePanel from '$lib/components/V2xPerspectivePanel.svelte';
	import {
		clientFps,
		connect,
		disconnect,
		driveConnected,
		endSession,
		lastError,
		sessionMode,
		sessionState,
		setOnFrame,
		startEditorSession
	} from '$lib/stores/driveSocket';
	import {
		GROUP_LABELS,
		actorDetails,
		cameraPose,
		distanceFromCamera,
		focusActor,
		followActor,
		followingId,
		requestWorldActors,
		resetEditorStore,
		selectActor,
		selectedActorId,
		sendCameraMove,
		worldActors,
		worldActorsInfo
	} from '$lib/stores/editor';
	import {
		bgTraffic,
		bgTrafficBusy,
		formatUptime,
		requestBgTraffic,
		resetBgTraffic,
		startBgTraffic,
		stopBgTraffic
	} from '$lib/stores/bgTraffic';
	import { TICK_MS, buildCameraMove, isFlyKey, mouseTurn, stepSpeed } from '$lib/flyInput';
	import { isTypingTarget } from '$lib/inputGate';
	import { loadRuntimeConfig, resolveWsUrl } from '$lib/runtime-config';
	import type { EditorStartPose, WorldActor } from '$lib/types';

	let cameraViewRef = $state<CameraViewComponent | null>(null);
	let cameraArea = $state<HTMLDivElement | null>(null);
	let started = $state(false);
	let search = $state('');
	let speed = $state(20);
	let speedDirty = false;
	let looking = $state(false);
	let dragging = false;
	let lastDrag = { x: 0, y: 0 };
	let mouseDx = 0;
	let mouseDy = 0;
	let note = $state('');
	const keys = new Set<string>();
	let tickTimer: ReturnType<typeof setInterval> | null = null;
	let listTimer: ReturnType<typeof setInterval> | null = null;

	// Background traffic (the SUMO bridge): status block with Start / Stop.
	let bgState = $derived($bgTraffic?.state ?? 'unavailable');
	let bgBusy = $derived($bgTrafficBusy !== null || bgState === 'starting' || bgState === 'stopping');
	let bgDot = $derived(
		bgState === 'running' ? 'bg-emerald-400' : bgState === 'starting' || bgState === 'stopping' ? 'bg-amber-400' : 'bg-gray-500'
	);
	let bgStatusText = $derived.by(() => {
		const s = $bgTraffic;
		if (!s) return '';
		if (s.state === 'running') return `running${s.uptime_s ? ` · ${formatUptime(s.uptime_s)}` : ''}`;
		if (s.state === 'starting') return 'starting…';
		if (s.state === 'stopping') return 'stopping… (parking the cars)';
		return 'stopped';
	});
	let bgNote = $derived.by(() => {
		const s = $bgTraffic;
		if (!s) return '';
		if (s.error) return s.error;
		if (s.last_start && !s.last_start.ok) return `start failed (exit ${s.last_start.code}): ${s.last_start.output.split('\n').at(-1) ?? ''}`;
		if (s.last_stop && !s.last_stop.ok) return `stop failed (exit ${s.last_stop.code}): ${s.last_stop.output.split('\n').at(-1) ?? ''}`;
		return '';
	});

	let active = $derived($driveConnected && $sessionState === 'driving' && $sessionMode === 'editor');
	let pose = $derived($cameraPose);
	let selected = $derived($worldActors.find((a) => a.id === $selectedActorId) ?? null);
	let filtered = $derived(
		$worldActors.filter((a) => {
			const q = search.trim().toLowerCase();
			if (!q) return true;
			return `${a.name} ${a.role} ${a.type_id} ${a.id} ${a.group}`.toLowerCase().includes(q);
		})
	);
	let grouped = $derived(
		Object.keys(GROUP_LABELS)
			.map((group) => ({ group, label: GROUP_LABELS[group], items: filtered.filter((a) => a.group === group) }))
			.filter((g) => g.items.length > 0)
	);

	function poseFromUrl(): EditorStartPose {
		if (typeof window === 'undefined') return {};
		const params = new URLSearchParams(window.location.search);
		const pose: EditorStartPose = {};
		for (const key of ['x', 'y', 'z', 'yaw', 'pitch'] as const) {
			const v = Number(params.get(key));
			if (params.has(key) && Number.isFinite(v)) pose[key] = v;
		}
		return pose;
	}

	// Start the editor session once the socket is up (and never restart on our own).
	// The store reports 'connecting' after the socket opens and 'idle' after a session ended.
	$effect(() => {
		if ($driveConnected && ($sessionState === 'idle' || $sessionState === 'connecting') && !started) {
			started = true;
			startEditorSession(poseFromUrl());
		}
	});

	// The server is the source of truth for the speed unless the user is changing it.
	$effect(() => {
		const p = $cameraPose;
		if (p && !speedDirty) speed = p.speed;
	});

	$effect(() => {
		if (active) requestWorldActors();
	});

	$effect(() => {
		if ($driveConnected) requestBgTraffic();
	});

	function tick() {
		if (!active) return;
		const msg = buildCameraMove({
			keys,
			...mouseTurn(mouseDx, mouseDy),
			dt: TICK_MS / 1000,
			speed: speedDirty ? speed : undefined,
			boost: keys.has('shift')
		});
		mouseDx = 0;
		mouseDy = 0;
		speedDirty = false;
		if (msg) sendCameraMove(msg);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (isTypingTarget(e.target)) return;
		const key = e.key.toLowerCase();
		if (isFlyKey(key)) {
			e.preventDefault();
			keys.add(key);
			return;
		}
		if (key === 'f' && selected) {
			focusActor(selected.id);
		} else if (key === 'escape') {
			if (document.pointerLockElement) document.exitPointerLock();
			else if (selected) selectActor(null);
		}
	}

	function handleKeyup(e: KeyboardEvent) {
		if (isTypingTarget(e.target)) return;
		// Space is "straight up": keep it from clicking the last focused button or scrolling
		if (e.key === ' ') e.preventDefault();
		keys.delete(e.key.toLowerCase());
	}

	function clearKeys() {
		keys.clear();
	}

	function handleMouseDown(e: MouseEvent) {
		if (e.button !== 2 && e.button !== 1) return;
		e.preventDefault();
		dragging = true;
		lastDrag = { x: e.clientX, y: e.clientY };
		try {
			cameraArea?.requestPointerLock?.();
		} catch {
			/* pointer lock unavailable: the drag fallback below still turns the camera */
		}
	}

	function handleMouseMove(e: MouseEvent) {
		if (document.pointerLockElement === cameraArea) {
			mouseDx += e.movementX;
			mouseDy += e.movementY;
		} else if (dragging) {
			mouseDx += e.clientX - lastDrag.x;
			mouseDy += e.clientY - lastDrag.y;
			lastDrag = { x: e.clientX, y: e.clientY };
		}
	}

	function handleMouseUp(e: MouseEvent) {
		if (e.button !== 2 && e.button !== 1) return;
		dragging = false;
		if (document.pointerLockElement === cameraArea) document.exitPointerLock();
	}

	function handlePointerLockChange() {
		looking = document.pointerLockElement === cameraArea;
		if (!looking) dragging = false;
	}

	function handleWheel(e: WheelEvent) {
		e.preventDefault();
		speed = stepSpeed(speed, e.deltaY < 0 ? 1 : -1);
		speedDirty = true;
	}

	function handleRowClick(actor: WorldActor) {
		selectActor($selectedActorId === actor.id ? null : actor.id);
	}

	async function copyPose() {
		if (!pose) return;
		const text = `x=${pose.x} y=${pose.y} z=${pose.z} yaw=${pose.yaw} pitch=${pose.pitch}`;
		try {
			await navigator.clipboard.writeText(text);
			note = 'Camera pose copied.';
		} catch {
			note = text;
		}
	}

	function switchToDriving() {
		const p = pose;
		endSession();
		const query = p ? `?x=${p.x}&y=${p.y}&yaw=${p.yaw}` : '';
		setTimeout(() => goto(`/drive${query}`), 150);
	}

	function handleEnd() {
		endSession();
		setTimeout(() => goto('/drive'), 150);
	}

	function toggleBgTraffic() {
		if (bgState === 'running') stopBgTraffic();
		else if (bgState === 'stopped') startBgTraffic();
		// the slot cars park or come back a few seconds later
		setTimeout(() => requestWorldActors(), 5000);
	}

	onMount(async () => {
		window.addEventListener('keydown', handleKeydown);
		window.addEventListener('keyup', handleKeyup);
		window.addEventListener('blur', clearKeys);
		document.addEventListener('pointerlockchange', handlePointerLockChange);
		tickTimer = setInterval(tick, TICK_MS);
		let ticks = 0;
		listTimer = setInterval(() => {
			if (active) requestWorldActors();
			if ($driveConnected && ++ticks % 3 === 0) requestBgTraffic();
		}, 1000);
		setOnFrame((blob: Blob) => {
			cameraViewRef?.pushFrame(blob);
		});
		const config = await loadRuntimeConfig();
		const url = resolveWsUrl(config.wsUrl);
		if (url) connect(url);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('keydown', handleKeydown);
			window.removeEventListener('keyup', handleKeyup);
			window.removeEventListener('blur', clearKeys);
			document.removeEventListener('pointerlockchange', handlePointerLockChange);
		}
		if (tickTimer) clearInterval(tickTimer);
		if (listTimer) clearInterval(listTimer);
		setOnFrame(null);
		if ($sessionState === 'driving' || $sessionState === 'reconstructing') endSession();
		disconnect();
		resetEditorStore();
		resetBgTraffic();
	});

	function fmt(v: number | null | undefined, digits = 1): string {
		return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '-';
	}
</script>

<svelte:head>
	<title>V2X Drive · Editor</title>
</svelte:head>

<div class="relative h-screen w-screen overflow-hidden bg-black font-mono text-[12px] leading-[18px] text-gray-100">
	<!-- camera -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		bind:this={cameraArea}
		class="absolute inset-y-0 left-[300px] right-[300px] {looking ? 'cursor-none' : 'cursor-crosshair'}"
		onmousedown={handleMouseDown}
		onmousemove={handleMouseMove}
		onmouseup={handleMouseUp}
		oncontextmenu={(e) => e.preventDefault()}
		onwheel={handleWheel}
		data-testid="editor-camera"
	>
		<CameraViewComponent bind:this={cameraViewRef} activeView="free" onSwitchView={() => {}} />
		{#if !active}
			<div class="absolute inset-0 flex items-center justify-center bg-gray-950/80">
				<div class="text-center">
					<p class="font-display text-lg font-bold tracking-widest text-white uppercase">
						{$lastError ? 'Editor unavailable' : 'Starting the editor'}
					</p>
					<p class="mt-2 text-xs text-gray-400">
						{$lastError ?? ($driveConnected ? 'Creating the free camera…' : 'Connecting to the drive server…')}
					</p>
					{#if $lastError}
						<a href="/drive" class="mt-4 inline-block rounded bg-gray-700 px-3 py-1 text-xs text-white hover:bg-gray-600">Back to driving</a>
					{/if}
				</div>
			</div>
		{/if}
		<div class="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 rounded-full border border-gray-700/70 bg-gray-900/90 px-4 py-1.5 text-[11px] font-semibold tracking-[0.16em] text-gray-300 uppercase">
			Editor
		</div>
		{#if $followingId !== null}
			<div class="pointer-events-none absolute top-12 left-1/2 -translate-x-1/2 rounded-full border border-cyan-700/60 bg-gray-900/90 px-3 py-1 text-[11px] text-cyan-200">
				following actor {$followingId} · any move releases
			</div>
		{/if}
	</div>

	<!-- left column: camera + details -->
	<aside class="absolute inset-y-0 left-0 z-20 flex w-[300px] flex-col overflow-y-auto bg-black/40 px-3 py-2 backdrop-blur-[2px]" data-testid="editor-left">
		<div class="pb-1 text-[10px] uppercase tracking-widest text-gray-500">Camera</div>
		<div class="flex justify-between"><span class="text-gray-400">Position:</span><span data-testid="editor-pos">({fmt(pose?.x)}, {fmt(pose?.y)}, {fmt(pose?.z)})</span></div>
		<div class="flex justify-between"><span class="text-gray-400">Yaw / pitch:</span><span>{fmt(pose?.yaw)}° / {fmt(pose?.pitch)}°</span></div>
		<div class="flex justify-between"><span class="text-gray-400">Speed:</span><span data-testid="editor-speed">{fmt(speed, 1)} m/s{keys.has('shift') ? ' ×4' : ''}</span></div>
		<div class="flex justify-between"><span class="text-gray-400">Frames:</span><span>{Math.round($clientFps)} FPS</span></div>
		<div class="mt-2 text-[10px] leading-[14px] text-gray-500">
			Right mouse button: look around · W A S D: fly · Space or E: straight up · Q: down · Shift: faster · wheel: speed · F: focus the selection
		</div>

		<div class="mt-4 pb-1 text-[10px] uppercase tracking-widest text-gray-500">Details</div>
		{#if selected}
			<div class="text-white">{selected.name} <span class="text-gray-500">#{selected.id}</span></div>
			<div class="text-[11px] text-gray-400">{selected.type_id}{selected.role ? ` · ${selected.role}` : ''}</div>
			<div class="text-[11px] text-gray-400">{GROUP_LABELS[selected.group] ?? selected.group} · owner {selected.owner}</div>
			{#if $actorDetails && $actorDetails.id === selected.id}
				{@const d = $actorDetails}
				<div class="mt-1 flex justify-between"><span class="text-gray-400">Position:</span><span>({fmt(d.pos?.[0])}, {fmt(d.pos?.[1])}, {fmt(d.pos?.[2])})</span></div>
				<div class="flex justify-between"><span class="text-gray-400">Rotation:</span><span>({fmt(d.rot?.[0])}, {fmt(d.rot?.[1])}, {fmt(d.rot?.[2])})</span></div>
				<div class="flex justify-between"><span class="text-gray-400">Speed:</span><span>{fmt((d.speed_mps ?? 0) * 3.6, 0)} km/h</span></div>
				{#if d.bbox}
					<div class="flex justify-between"><span class="text-gray-400">Half extents:</span><span>({fmt(d.bbox[0])}, {fmt(d.bbox[1])}, {fmt(d.bbox[2])})</span></div>
				{/if}
				{#if d.control}
					<div class="flex justify-between"><span class="text-gray-400">Control:</span><span>t {fmt(d.control.throttle, 2)} s {fmt(d.control.steer, 2)} b {fmt(d.control.brake, 2)}</span></div>
					<div class="flex justify-between"><span class="text-gray-400">Gear:</span><span>{d.control.gear}{d.control.hand_brake ? ' · hand brake' : ''}{d.control.reverse ? ' · reverse' : ''}</span></div>
				{/if}
				{#if d.lights}
					<div class="flex justify-between"><span class="text-gray-400">Lights:</span><span class="truncate pl-2">{d.lights.length ? d.lights.join(', ') : 'off'}</span></div>
				{/if}
				{#if typeof d.speed_limit === 'number'}
					<div class="flex justify-between"><span class="text-gray-400">Speed limit:</span><span>{d.speed_limit} km/h</span></div>
				{/if}
				{#if d.parent !== null}
					<div class="flex justify-between"><span class="text-gray-400">Attached to:</span><span>#{d.parent} {d.parent_type ?? ''}</span></div>
				{/if}
				{#each Object.entries(d.attributes) as [key, value] (key)}
					<div class="flex justify-between text-[11px]"><span class="text-gray-500">{key}:</span><span class="truncate pl-2 text-gray-300">{value}</span></div>
				{/each}
			{:else}
				<div class="text-gray-500">loading details…</div>
			{/if}
			<div class="mt-2 flex flex-wrap gap-1">
				<button type="button" onclick={() => focusActor(selected!.id)} class="cursor-pointer rounded bg-white/10 px-2 py-0.5 text-white hover:bg-white/20" data-testid="editor-focus">Focus (F)</button>
				{#if $followingId === selected.id}
					<button type="button" onclick={() => followActor(null)} class="cursor-pointer rounded bg-cyan-700/70 px-2 py-0.5 text-white hover:bg-cyan-600">Unfollow</button>
				{:else}
					<button type="button" onclick={() => followActor(selected!.id)} class="cursor-pointer rounded bg-white/10 px-2 py-0.5 text-white hover:bg-white/20" data-testid="editor-follow">Follow</button>
				{/if}
				<button type="button" onclick={copyPose} class="cursor-pointer rounded bg-white/10 px-2 py-0.5 text-white hover:bg-white/20">Copy camera pose</button>
			</div>
		{:else}
			<div class="text-gray-500">Pick an actor in the outliner.</div>
		{/if}
		{#if note}
			<div class="mt-2 truncate text-[11px] text-amber-300" role="status">{note}</div>
		{/if}
		<div class="mt-3 border-t border-white/10 pt-2">
			<V2xPerspectivePanel />
		</div>
		<div class="mt-auto pt-3 text-[10px] text-gray-500">read-only: the editor never moves or deletes anything but its camera</div>
	</aside>

	<!-- right column: outliner -->
	<aside class="absolute inset-y-0 right-0 z-20 flex w-[300px] flex-col bg-black/40 px-2 py-2 backdrop-blur-[2px]" data-testid="editor-outliner">
		<div class="flex items-center justify-between px-2 pb-1">
			<span class="text-[10px] uppercase tracking-widest text-gray-500">Outliner · {$worldActorsInfo.total}{$worldActorsInfo.truncated ? '+' : ''}</span>
			<button type="button" onclick={() => requestWorldActors()} class="cursor-pointer text-[10px] text-gray-400 hover:text-white">refresh</button>
		</div>
		<input
			type="search"
			bind:value={search}
			placeholder="filter by name, role, type…"
			aria-label="Filter actors"
			class="mx-2 mb-1 rounded border border-gray-700 bg-gray-900/80 px-2 py-1 text-[11px] text-gray-100 placeholder-gray-500 focus:border-cyan-500 focus:outline-none"
		/>
		<div class="min-h-0 flex-1 overflow-y-auto">
			{#each grouped as g (g.group)}
				<div class="px-2 pb-0.5 pt-2 text-[10px] uppercase tracking-widest text-gray-500">{g.label} · {g.items.length}</div>
				{#each g.items as actor (actor.id)}
					<button
						type="button"
						onclick={() => handleRowClick(actor)}
						ondblclick={() => focusActor(actor.id)}
						aria-pressed={$selectedActorId === actor.id}
						class="flex w-full cursor-pointer items-center gap-2 rounded px-2 py-[2px] text-left {$selectedActorId === actor.id ? 'bg-white/15 text-white' : 'text-gray-200 hover:bg-white/10'}"
						data-testid="outliner-row"
					>
						<span class="truncate">{actor.name}</span>
						{#if actor.role}<span class="truncate text-[10px] text-gray-500">{actor.role}</span>{/if}
						<span class="ml-auto shrink-0 text-[10px] text-gray-400">
							{#if distanceFromCamera(pose, actor) !== null}{Math.round(distanceFromCamera(pose, actor)!)} m{/if}
							{#if $followingId === actor.id}<span class="text-cyan-300"> · following</span>{/if}
						</span>
					</button>
				{/each}
			{:else}
				<div class="px-2 py-4 text-gray-500">{$worldActors.length ? 'nothing matches the filter' : 'no actors listed yet'}</div>
			{/each}
		</div>
		{#if $bgTraffic?.available}
			<div class="mt-2 border-t border-white/10 px-2 pt-2" data-testid="editor-bg-traffic">
				<div class="pb-1 text-[10px] uppercase tracking-widest text-gray-500">Background traffic</div>
				<div class="flex items-center gap-2">
					<span class="inline-block h-1.5 w-1.5 shrink-0 rounded-full {bgDot}"></span>
					<span class="truncate text-gray-200" title={$bgTraffic.label} data-testid="editor-bg-state">{bgStatusText}</span>
					<button
						type="button"
						onclick={toggleBgTraffic}
						disabled={bgBusy}
						class="ml-auto shrink-0 cursor-pointer rounded bg-white/10 px-2 py-0.5 text-white hover:bg-white/20 disabled:cursor-default disabled:opacity-50"
						data-testid="editor-bg-toggle"
					>
						{bgState === 'running' || bgState === 'stopping' ? 'Stop' : 'Start'}
					</button>
				</div>
				{#if $bgTraffic.last_line}
					<div class="truncate pt-0.5 text-[10px] text-gray-500" title={$bgTraffic.last_line}>{$bgTraffic.last_line}</div>
				{/if}
				{#if bgNote}<div class="pt-0.5 text-[10px] text-amber-300">{bgNote}</div>{/if}
			</div>
		{/if}
		<div class="mt-2 border-t border-white/10 px-2 pt-2">
			<div class="pb-1 text-[10px] uppercase tracking-widest text-gray-500">Session</div>
			<button type="button" onclick={switchToDriving} class="flex w-full cursor-pointer items-center gap-2 rounded px-2 py-[3px] text-left text-gray-200 hover:bg-white/10 hover:text-white" data-testid="editor-switch-driving">
				<span class="inline-block h-1.5 w-1.5 rounded-full bg-gray-500"></span>Switch to driving here
			</button>
			<button type="button" onclick={handleEnd} class="flex w-full cursor-pointer items-center gap-2 rounded px-2 py-[3px] text-left text-red-300 hover:bg-white/10 hover:text-white" data-testid="editor-end">
				<span class="inline-block h-1.5 w-1.5 rounded-full bg-red-400"></span>End editor
			</button>
		</div>
	</aside>
</div>
