<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { fetchDemoVideos } from '$lib/api';
	import type { DemoVideo } from '$lib/types';

	let videos = $state<DemoVideo[]>([]);
	let isLoading = $state(true);
	let error = $state<string | null>(null);
	let selectedName = $state<string | null>(null);
	let durations = $state<Record<string, number>>({});
	let player = $state<HTMLVideoElement | null>(null);
	let stage = $state<HTMLElement | null>(null);

	const selected = $derived(videos.find((video) => video.fileName === selectedName) ?? videos[0] ?? null);

	function formatDuration(seconds: number | undefined): string {
		if (!seconds || !Number.isFinite(seconds)) return '';
		const total = Math.round(seconds);
		const minutes = Math.floor(total / 60);
		return `${minutes}:${String(total % 60).padStart(2, '0')}`;
	}

	function formatDate(value: string | null): string {
		if (!value) return '';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '';
		return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function rememberDuration(fileName: string, event: Event) {
		const duration = (event.currentTarget as HTMLVideoElement).duration;
		if (Number.isFinite(duration)) durations[fileName] = duration;
	}

	async function select(video: DemoVideo) {
		if (video.fileName === selected?.fileName) {
			player?.play();
			return;
		}
		selectedName = video.fileName;
		const url = new URL(page.url);
		url.searchParams.set('v', video.fileName);
		replaceState(url, {});
		await tick();
		stage?.scrollIntoView({ behavior: 'smooth', block: 'start' });
		player?.play().catch(() => {});
	}

	async function loadVideos() {
		error = null;
		isLoading = true;
		try {
			videos = await fetchDemoVideos();
			const requested = page.url.searchParams.get('v');
			if (requested && videos.some((video) => video.fileName === requested)) selectedName = requested;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to fetch demo videos.';
		} finally {
			isLoading = false;
		}
	}

	onMount(loadVideos);
</script>

<svelte:head>
	<title>V2X Demo Videos</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950 text-white">
	<header class="flex h-14 shrink-0 items-center justify-between border-b border-gray-800 bg-gray-950/80 px-4 backdrop-blur-sm">
		<a href="/" class="flex items-center gap-3" aria-label="V2X home">
			<img src="/logo.png" alt="V2X logo" class="h-8" />
			<div>
				<h1 class="text-sm font-semibold text-white">V2X</h1>
				<p class="text-[10px] text-gray-500">Richmond Field Station</p>
			</div>
		</a>
		<a
			href="/"
			class="rounded-full border border-gray-700/70 bg-gray-900 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors hover:border-gray-600 hover:text-white"
		>
			&larr; Home
		</a>
	</header>

	<main class="min-h-0 flex-1 overflow-y-auto">
		<div class="mx-auto flex max-w-7xl flex-col gap-8 px-4 py-6">
			<div class="space-y-1">
				<p class="text-xs font-medium uppercase tracking-[0.3em] text-cyan-300/80">Media library</p>
				<h1 class="text-3xl font-semibold">Demo Videos</h1>
				<p class="max-w-3xl text-sm text-gray-400">
					Recorded demos from Richmond Field Station: V2X alerts in the drive simulator, driver-in-the-loop trials and the
					pole-camera detection pipeline.
				</p>
			</div>

			{#if error}
				<div class="flex flex-wrap items-center justify-between gap-3 rounded-3xl border border-red-500/30 bg-red-500/10 px-5 py-4 text-sm text-red-200">
					<span>{error}</span>
					<button
						type="button"
						onclick={loadVideos}
						class="rounded-full border border-red-400/40 px-4 py-1.5 text-xs font-medium transition hover:border-red-300"
					>
						Try again
					</button>
				</div>
			{:else if isLoading}
				<div class="aspect-video w-full animate-pulse rounded-3xl border border-gray-800 bg-gray-900/70" aria-hidden="true"></div>
			{:else if !selected}
				<div class="rounded-3xl border border-gray-800 bg-gray-900/70 px-6 py-10 text-center text-sm text-gray-400">
					No demo videos have been uploaded yet.
				</div>
			{:else}
				<section
					bind:this={stage}
					class="scroll-mt-6 overflow-hidden rounded-3xl border border-gray-800 bg-gray-900/70 shadow-[0_20px_80px_rgba(0,0,0,0.35)]"
				>
					{#key selected.fileName}
						<!-- Captions, where a demo has them, are burned into the video; there are no sidecar tracks. -->
						<!-- svelte-ignore a11y_media_has_caption -->
						<video
							bind:this={player}
							src={selected.url}
							poster={selected.posterUrl ?? undefined}
							class="aspect-video w-full bg-black object-contain"
							controls
							playsinline
							preload="metadata"
							onloadedmetadata={(event) => rememberDuration(selected.fileName, event)}
						>
							Your browser cannot play this video.
						</video>
					{/key}
					<div class="flex flex-wrap items-center justify-between gap-3 px-6 py-5">
						<div>
							<h2 class="text-2xl font-semibold">{selected.title}</h2>
							<p class="text-sm text-gray-400">
								{[formatDuration(durations[selected.fileName]), formatDate(selected.lastModified)].filter(Boolean).join(' · ')}
							</p>
						</div>
						<a
							href={selected.url}
							download={selected.fileName}
							class="inline-flex items-center gap-2 rounded-full border border-cyan-400/40 bg-cyan-400/10 px-4 py-2 text-sm font-medium text-cyan-100 transition hover:border-cyan-300 hover:bg-cyan-300/15"
						>
							Download <span aria-hidden="true">&darr;</span>
						</a>
					</div>
				</section>
			{/if}

			{#if videos.length > 1}
				<section class="space-y-3">
					<h2 class="text-lg font-semibold">All videos <span class="text-gray-500">{videos.length}</span></h2>
					<div class="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
						{#each videos as video (video.fileName)}
							{@const active = video.fileName === selected?.fileName}
							<button
								type="button"
								onclick={() => select(video)}
								aria-current={active ? 'true' : undefined}
								class={`group flex flex-col overflow-hidden rounded-3xl border bg-gray-900/70 text-left transition hover:bg-gray-900 ${active ? 'border-cyan-400/60' : 'border-gray-800 hover:border-cyan-400/50'}`}
							>
								<div class="relative aspect-video overflow-hidden border-b border-gray-800 bg-black">
									<video
										src={video.posterUrl ? video.url : `${video.url}#t=1`}
										poster={video.posterUrl ?? undefined}
										class="pointer-events-none h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]"
										muted
										playsinline
										preload="metadata"
										tabindex="-1"
										aria-hidden="true"
										onloadedmetadata={(event) => rememberDuration(video.fileName, event)}
									></video>
									{#if active}
										<span class="absolute left-3 top-3 rounded-full bg-cyan-400/90 px-2.5 py-1 text-[11px] font-semibold text-gray-950">Now playing</span>
									{/if}
									{#if durations[video.fileName]}
										<span class="absolute bottom-3 right-3 rounded-full bg-black/75 px-2.5 py-1 font-mono text-[11px] text-gray-200">
											{formatDuration(durations[video.fileName])}
										</span>
									{/if}
								</div>
								<div class="flex flex-col gap-1 px-5 py-4">
									<h3 class="font-semibold">{video.title}</h3>
									<p class="text-xs text-gray-500">{formatDate(video.lastModified)}</p>
								</div>
							</button>
						{/each}
					</div>
				</section>
			{/if}
		</div>
	</main>
</div>
