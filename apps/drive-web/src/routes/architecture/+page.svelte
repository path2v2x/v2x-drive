<script lang="ts">
	interface Node {
		title: string;
		detail: string;
		mono?: string;
	}

	interface Host {
		name: string;
		where: string;
		accent: string;
		nodes: Node[];
	}

	const hosts: Host[] = [
		{
			name: 'Your browser',
			where: 'anywhere on the internet',
			accent: 'border-cyan-400/40 bg-cyan-400/5',
			nodes: [
				{ title: 'This website', detail: 'Drive, Street View Live, Timeline, Demo Videos', mono: 'https://path2v2x.net' },
				{ title: 'Digital twin', detail: 'SimForge-based twin of Richmond Field Station', mono: 'https://twin.path2v2x.net' }
			]
		},
		{
			name: 'RFS PC (path-rfs)',
			where: 'Richmond Field Station, UC Berkeley campus network',
			accent: 'border-emerald-400/40 bg-emerald-400/5',
			nodes: [
				{ title: 'nginx + Let\u2019s Encrypt', detail: 'Serves this site and fronts every service below on one origin', mono: 'https://path2v2x.net · https://twin.path2v2x.net' },
				{ title: 'CARLA 0.10 (Unreal 5)', detail: 'Richmond Field Station map, Docker container, RTX 5080', mono: 'RPC :2000-2002' },
				{ title: 'Drive server', detail: 'Owns the CARLA tick (20 Hz sync mode), ego vehicle, cameras, scenarios; publishes state.json and map data', mono: '/ws · /data/' },
				{ title: 'Twin server', detail: 'SimForge OSS engine mirroring live detections; keeps 72 h of detection history in SQLite for replay and the Timeline', mono: '/twin · /detections/' },
				{ title: 'Perception (co-perception)', detail: 'YOLOv8 on the four pole cameras, GPS projection of every detection', mono: '127.0.0.1:8091' },
				{ title: 'Camera relay (MediaMTX)', detail: 'H.264 pass-through; local low-latency HLS and 72-hour (3-day) recording playback from the archive NVMe for browsers, RTSP for the twin', mono: '/camera/chN/index.m3u8 · /archive/' }
			]
		},
		{
			name: 'AWS',
			where: 'PATH AWS account',
			accent: 'border-amber-400/40 bg-amber-400/5',
			nodes: [
				{ title: 'Route 53', detail: 'DNS only: path2v2x.net, www, drive and twin all point at the RFS PC' }
			]
		},
		{
			name: 'Field cameras',
			where: 'Richmond Field Station pole, via the PeMS camera server',
			accent: 'border-fuchsia-400/40 bg-fuchsia-400/5',
			nodes: [{ title: '4 \u00d7 2560\u00d71920 H.264 cameras', detail: 'One RTSP session per camera into the RFS PC; video never leaves path-rfs', mono: 'ch1 \u2013 ch4' }]
		}
	];

	const flows = [
		{ from: 'Browser', to: 'nginx (RFS PC)', what: 'Static site: HTML/JS/config.json', how: 'https://path2v2x.net' },
		{ from: 'Browser', to: 'Drive server (RFS PC)', what: 'Drive session: controls, HUD, CARLA camera frames', how: 'wss://path2v2x.net/ws' },
		{ from: 'Browser', to: 'Camera relay (RFS PC)', what: 'Live and archived camera video', how: '/camera/chN/index.m3u8 (HLS) · /archive/get (MP4)' },
		{ from: 'Browser', to: 'Twin server (RFS PC)', what: 'Detection history for the Timeline (72 h)', how: '/detections/coverage · /objects · /history' },
		{ from: 'Browser', to: 'Drive server (RFS PC)', what: 'Heartbeat, map data, demo videos', how: '/data/api/state.json · /data/api/map-data.json · /data/demo-videos/' },
		{ from: 'Browser', to: 'Twin server (RFS PC)', what: 'Digital twin truth frames, camera feeds, replay clock', how: 'wss://twin.path2v2x.net/twin' },
		{ from: 'Cameras', to: 'RFS PC', what: 'Raw H.264 (one session per camera), fanned out on the host to perception, relay and recording', how: 'RTSP over TCP' },
		{ from: 'Perception (RFS PC)', to: 'Twin server (RFS PC)', what: 'Per-camera detections with GPS position, recorded for 72 h', how: 'http://127.0.0.1:8091/detections/latest' },
		{ from: 'Drive server (RFS PC)', to: 'CARLA (RFS PC)', what: 'World tick, ego control, sensors', how: 'CARLA RPC, localhost:2000' },
		{ from: 'SUMO / VOICES / HIL tools', to: 'CARLA (RFS PC)', what: 'Sidecar clients sharing the same world', how: 'CARLA RPC :2000 (see below)' }
	];
</script>

<svelte:head>
	<title>V2X Drive Architecture</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950">
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
				<p class="text-xs font-medium uppercase tracking-[0.3em] text-cyan-300/80">System overview</p>
				<h1 class="text-3xl font-semibold text-white">Architecture</h1>
				<p class="max-w-3xl text-sm text-gray-400">
					Everything runs on one workstation at Richmond Field Station (the RFS PC): this website, the CARLA drive
					server, the digital twin, perception, and the camera relay. The browser talks to that one host; AWS only
					holds DNS.
				</p>
			</div>

			<!-- Diagram -->
			<section class="space-y-3">
				<div class="grid gap-4 lg:grid-cols-4">
					{#each hosts as host}
						<div class={`flex flex-col gap-3 rounded-3xl border p-4 ${host.accent}`}>
							<div>
								<h2 class="text-sm font-semibold text-white">{host.name}</h2>
								<p class="text-[11px] text-gray-400">{host.where}</p>
							</div>
							{#each host.nodes as node}
								<div class="rounded-2xl border border-gray-800 bg-gray-950/80 px-3 py-2.5">
									<p class="text-sm font-medium text-white">{node.title}</p>
									<p class="text-xs text-gray-400">{node.detail}</p>
									{#if node.mono}
										<p class="mt-1 font-mono text-[11px] text-cyan-200/80">{node.mono}</p>
									{/if}
								</div>
							{/each}
						</div>
					{/each}
				</div>
				<p class="text-xs text-gray-500">
					Browser &rarr; RFS PC for everything: this site, drive sessions, the twin, detection history, and both live and 72-hour (3-day) archived camera video;
					Cameras &rarr; RFS PC for video. The only cloud dependency left is DNS.
				</p>
			</section>

			<!-- Connections -->
			<section class="space-y-3">
				<h2 class="text-lg font-semibold text-white">Who talks to whom</h2>
				<div class="overflow-x-auto rounded-3xl border border-gray-800 bg-gray-900/70">
					<table class="w-full text-left text-sm">
						<thead class="border-b border-gray-800 text-[11px] uppercase tracking-[0.16em] text-gray-500">
							<tr>
								<th class="px-4 py-3">From</th>
								<th class="px-4 py-3">To</th>
								<th class="px-4 py-3">What</th>
								<th class="px-4 py-3">How</th>
							</tr>
						</thead>
						<tbody class="divide-y divide-gray-800/80 text-gray-300">
							{#each flows as flow}
								<tr>
									<td class="px-4 py-2.5 whitespace-nowrap text-white">{flow.from}</td>
									<td class="px-4 py-2.5 whitespace-nowrap text-white">{flow.to}</td>
									<td class="px-4 py-2.5">{flow.what}</td>
									<td class="px-4 py-2.5 font-mono text-xs text-cyan-200/80">{flow.how}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>

			<!-- Connecting external simulators -->
			<section class="grid gap-6 lg:grid-cols-2">
				<div class="space-y-4 rounded-3xl border border-gray-800 bg-gray-900/70 p-5">
					<h2 class="text-lg font-semibold text-white">Connecting SUMO (or any CARLA client)</h2>
					<p class="text-sm text-gray-400">
						CARLA on the RFS PC is one shared world. External tools attach as <span class="text-white">sidecar clients</span>
						on the standard CARLA RPC port; the drive server keeps owning the browser session and the simulation tick.
					</p>
					<ol class="list-decimal space-y-3 pl-5 text-sm text-gray-300">
						<li>
							<span class="text-white">Get network access.</span> Port 2000 is not open to the public internet.
							Either run your client on the RFS PC itself, or join the PATH Tailscale network (ask the V2X team for an invite)
							and use the RFS PC's Tailscale address.
						</li>
						<li>
							<span class="text-white">Point your client at CARLA.</span>
							<pre class="mt-2 overflow-x-auto rounded-2xl border border-gray-800 bg-gray-950 px-4 py-3 font-mono text-xs text-cyan-100">On the RFS PC:        host=localhost         port=2000
Over Tailscale:       host=100.126.56.83     port=2000

# Python (CARLA 0.10 PythonAPI)
client = carla.Client(host, 2000)
client.set_timeout(10.0)
world = client.get_world()   # Richmond_Field_Station_Richmond_CA

# SUMO co-simulation (CARLA Co-Simulation/Sumo)
python run_synchronization.py your_network.sumocfg --carla-host $HOST --carla-port 2000</pre>
						</li>
						<li>
							<span class="text-white">Do not take over the tick.</span> The drive server already runs CARLA in synchronous
							mode at 20 Hz. Sidecar clients must not switch the world to their own synchronous settings or call
							<span class="font-mono text-cyan-200/80">world.tick()</span>; spawn, read and update actors only.
							Use a distinct <span class="font-mono text-cyan-200/80">role_name</span> for your actors so they are not
							mistaken for drive-owned vehicles.
						</li>
						<li>
							<span class="text-white">Coordinate with the nightly restart.</span> CARLA and the drive server restart at 04:00
							Pacific unless a drive session is active; long-running clients should reconnect automatically.
						</li>
					</ol>
				</div>

				<div class="space-y-4 rounded-3xl border border-gray-800 bg-gray-900/70 p-5">
					<h2 class="text-lg font-semibold text-white">Distributed testing (VOICES)</h2>
					<p class="text-sm text-gray-400">
						For cross-institution distributed testing, PATH exposes this CARLA world through the VOICES Docker portal.
						The VOICES container runs on the RFS PC next to CARLA and reaches it over the local Docker network,
						so a direct SUMO connection (above) and a VOICES connection can run at the same time on the same world.
					</p>
					<div class="rounded-2xl border border-gray-800 bg-gray-950 px-4 py-3 text-xs text-gray-300">
						<p><span class="text-white">One CARLA, many clients:</span> drive server (browser), SUMO, VOICES portal, HIL tools.</p>
						<p class="mt-1"><span class="text-white">Not exposed:</span> ports 2000-2002 (CARLA) and 8765 (drive WebSocket) are firewalled from the internet; only nginx on 443 is public.</p>
					</div>
					<h3 class="pt-2 text-sm font-semibold text-white">Source code</h3>
					<ul class="space-y-1 text-sm">
						<li><a class="text-cyan-300 hover:underline" href="https://github.com/path2v2x/v2x-drive" target="_blank" rel="noreferrer">path2v2x/v2x-drive</a> <span class="text-gray-500">&mdash; this site, the drive server, deployment</span></li>
						<li><a class="text-cyan-300 hover:underline" href="https://github.com/path2v2x/v2x-digital-twin" target="_blank" rel="noreferrer">path2v2x/v2x-digital-twin</a> <span class="text-gray-500">&mdash; SimForge-based twin</span></li>
						<li><a class="text-cyan-300 hover:underline" href="https://github.com/path2v2x/co-perception" target="_blank" rel="noreferrer">path2v2x/co-perception</a> <span class="text-gray-500">&mdash; camera perception</span></li>
						<li><a class="text-cyan-300 hover:underline" href="https://github.com/SimForgeinc/simforge-oss" target="_blank" rel="noreferrer">SimForgeinc/simforge-oss</a> <span class="text-gray-500">&mdash; simulation engine used by the twin</span></li>
					</ul>
				</div>
			</section>
		</div>
	</main>
</div>
