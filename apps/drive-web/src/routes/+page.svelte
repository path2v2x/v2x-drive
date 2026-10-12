<script lang="ts">
	interface Card {
		title: string;
		blurb: string;
		href: string;
		image: string;
		external: boolean;
		label: string;
	}

	const cards: Card[] = [
		{
			title: 'Drive',
			blurb: 'Drive a vehicle through Richmond Field Station in CARLA, with live pole cameras, V2X zones and scenarios.',
			href: '/drive',
			image: '/landing/drive.webp',
			external: false,
			label: 'Open the drive app'
		},
		{
			title: 'Digital Twin',
			blurb: 'The SimForge-based twin of the field station: live detections mirrored onto the map, replay, and camera views.',
			href: 'https://twin.path2v2x.net/',
			image: '/landing/twin.webp',
			external: true,
			label: 'Open the digital twin'
		},
		{
			title: 'Architecture',
			blurb: 'How the pieces fit: this site on AWS, CARLA and perception on the RFS PC, and how to attach SUMO or VOICES.',
			href: '/architecture',
			image: '/landing/architecture.webp',
			external: false,
			label: 'See the architecture'
		},
		{
			title: 'Demo Videos',
			blurb: 'Recorded demos from the field station: V2X emergency-vehicle alerts, driver-in-the-loop trials and street-camera detections.',
			href: '/demo-videos',
			image: '/landing/demo-videos.webp',
			external: false,
			label: 'Watch the demos'
		}
	];
</script>

<svelte:head>
	<title>V2X Drive - Richmond Field Station</title>
</svelte:head>

<div class="min-h-screen bg-gray-950 text-white">
	<main class="mx-auto flex min-h-screen max-w-7xl flex-col justify-center gap-10 px-6 py-12">
		<div class="flex items-center gap-4">
			<img src="/logo.png" alt="V2X logo" class="h-12" />
			<div>
				<h1 class="text-3xl font-semibold">V2X Drive</h1>
				<p class="text-sm text-gray-400">Richmond Field Station, UC Berkeley PATH</p>
			</div>
		</div>

		<div class="grid gap-6 md:grid-cols-2 xl:grid-cols-4">
			{#each cards as card}
				<a
					href={card.href}
					target={card.external ? '_blank' : undefined}
					rel={card.external ? 'noreferrer' : undefined}
					class="group flex flex-col overflow-hidden rounded-3xl border border-gray-800 bg-gray-900/70 shadow-[0_20px_80px_rgba(0,0,0,0.35)] transition hover:border-cyan-400/50 hover:bg-gray-900"
				>
					<div class="aspect-video overflow-hidden border-b border-gray-800 bg-black">
						<img
							src={card.image}
							alt={`${card.title} preview`}
							class="h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]"
							loading="eager"
						/>
					</div>
					<div class="flex flex-1 flex-col gap-3 px-6 py-6">
						<h2 class="text-2xl font-semibold">{card.title}</h2>
						<p class="flex-1 text-sm text-gray-400">{card.blurb}</p>
						<span class="inline-flex w-fit items-center gap-2 rounded-full border border-cyan-400/40 bg-cyan-400/10 px-4 py-2 text-sm font-medium text-cyan-100 transition group-hover:border-cyan-300 group-hover:bg-cyan-300/15">
							{card.label}
							{#if card.external}
								<span aria-hidden="true">&#8599;</span>
							{:else}
								<span aria-hidden="true">&rarr;</span>
							{/if}
						</span>
					</div>
				</a>
			{/each}
		</div>
	</main>
</div>
