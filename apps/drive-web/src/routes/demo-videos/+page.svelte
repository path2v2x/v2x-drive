<script lang="ts">
	import { onMount } from 'svelte';
	import Header from '$lib/components/Header.svelte';
	import { fetchDemoVideos } from '$lib/api';
	import type { DemoVideo } from '$lib/types';

	let videos = $state<DemoVideo[]>([]);
	let isLoading = $state(true);
	let error = $state<string | null>(null);

	function formatBytes(sizeBytes: number): string {
		if (!sizeBytes) return '0 B';
		const units = ['B', 'KB', 'MB', 'GB'];
		let value = sizeBytes;
		let unitIndex = 0;
		while (value >= 1024 && unitIndex < units.length - 1) {
			value /= 1024;
			unitIndex += 1;
		}
		const digits = value >= 100 || unitIndex === 0 ? 0 : 1;
		return `${value.toFixed(digits)} ${units[unitIndex]}`;
	}

	function formatTimestamp(value: string | null): string {
		if (!value) return 'Unknown upload time';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return value;
		return date.toLocaleString();
	}

	async function loadVideos() {
		error = null;
		isLoading = true;
		try {
			videos = await fetchDemoVideos();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to fetch demo videos.';
		} finally {
			isLoading = false;
		}
	}

	onMount(async () => {
		await loadVideos();
	});
</script>

<svelte:head>
	<title>V2X Drive Demo Videos</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950">
	<Header />

	<main class="min-h-0 flex-1 overflow-y-auto">
		<div class="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-6">
			<div class="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
				<div class="space-y-1">
					<p class="text-xs font-medium uppercase tracking-[0.3em] text-cyan-300/80">Media library</p>
					<h1 class="text-3xl font-semibold text-white">Demo Videos</h1>
					<p class="max-w-3xl text-sm text-gray-400">
						Field demos, screen captures, and scenario walkthroughs stored privately in S3 and streamed to this page with signed URLs.
					</p>
				</div>
				<div class="rounded-2xl border border-gray-800 bg-gray-900/70 px-4 py-3 text-sm text-gray-300">
					<span class="font-medium text-white">{videos.length}</span> videos available
				</div>
			</div>

			{#if error}
				<div class="rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
					{error}
				</div>
			{/if}

			{#if isLoading}
				<div class="grid gap-5 md:grid-cols-2">
					{#each Array.from({ length: 4 }) as _, index}
						<div class="overflow-hidden rounded-3xl border border-gray-800 bg-gray-900/70" aria-hidden="true">
							<div class="aspect-video animate-pulse bg-gray-800/80"></div>
							<div class="space-y-3 px-5 py-5">
								<div class="h-4 w-2/3 animate-pulse rounded bg-gray-800"></div>
								<div class="h-3 w-1/3 animate-pulse rounded bg-gray-800"></div>
								<div class="h-9 w-28 animate-pulse rounded-full bg-gray-800"></div>
							</div>
						</div>
					{/each}
				</div>
			{:else if videos.length === 0}
				<div class="rounded-3xl border border-gray-800 bg-gray-900/70 px-6 py-10 text-center text-sm text-gray-400">
					No demo videos have been uploaded yet.
				</div>
			{:else}
				<div class="grid gap-6 md:grid-cols-2">
					{#each videos as video}
						<article class="overflow-hidden rounded-3xl border border-gray-800 bg-gray-900/70 shadow-[0_20px_80px_rgba(0,0,0,0.35)]">
							<div class="border-b border-gray-800 bg-gradient-to-br from-cyan-400/10 via-transparent to-blue-500/10">
								<video
									class="aspect-video w-full bg-black"
									controls
									preload="metadata"
									playsinline
								>
									<source src={video.url} />
									Your browser cannot play this video. Use the open link below.
								</video>
							</div>
							<div class="space-y-4 px-5 py-5">
								<div class="space-y-2">
									<h2 class="text-lg font-semibold text-white">{video.title}</h2>
									<p class="font-mono text-xs text-gray-500">{video.fileName}</p>
								</div>

								<div class="flex flex-wrap gap-2 text-xs text-gray-300">
									<span class="rounded-full border border-gray-700 bg-gray-950 px-3 py-1">
										{formatBytes(video.sizeBytes)}
									</span>
									<span class="rounded-full border border-gray-700 bg-gray-950 px-3 py-1">
										Uploaded {formatTimestamp(video.lastModified)}
									</span>
								</div>

								<div class="flex items-center gap-3">
									<a
										href={video.url}
										target="_blank"
										rel="noreferrer"
										class="rounded-full border border-cyan-400/40 bg-cyan-400/10 px-4 py-2 text-sm font-medium text-cyan-100 transition hover:border-cyan-300 hover:bg-cyan-300/15"
									>
										Open in new tab
									</a>
								</div>
							</div>
						</article>
					{/each}
				</div>
			{/if}
		</div>
	</main>
</div>
