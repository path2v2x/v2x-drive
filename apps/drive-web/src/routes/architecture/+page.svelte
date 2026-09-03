<script lang="ts">
	import MermaidDiagram from '$lib/components/MermaidDiagram.svelte';

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

	const systemDiagram = `flowchart LR
  browser([Browser])
  cams[/"4 pole cameras<br/>2560x1920 H.264"/]
  subgraph rfs["RFS PC (path-rfs) - one nginx on :443, everything else on loopback"]
    direction LR
    subgraph drive["path2v2x/v2x-drive"]
      web["drive-web<br/>static site<br/>/ /drive /live /timeline"]
      ds["drive-server<br/>:8765 /ws<br/>publishes /data/"]
      carla["CARLA 0.10<br/>:2000 (Docker)"]
      mtx["MediaMTX relay + recorder<br/>:8888 /camera/<br/>:9996 /archive/ (72 h)"]
    end
    subgraph twin["path2v2x/v2x-digital-twin"]
      ts["twin-server<br/>:8865 /twin /drive /camera-feeds<br/>:8190 /detections/ (72 h SQLite)"]
      perc["v2x-perception service<br/>runs co-perception<br/>:8091 /detections/latest"]
    end
    subgraph sf["SimForgeinc/simforge-oss"]
      studio["Studio Drive UI<br/>:5199 /dashboard/drive"]
    end
    subgraph cp["path2v2x/co-perception (jpark)"]
      demux["camera-pipeline demux<br/>/tmp/camera_demux_chN.sock"]
    end
  end
  cams -->|RTSP, one session per camera| demux
  demux -->|H.264 access units| mtx
  demux -->|H.264 access units| perc
  perc -->|10 Hz poll| ts
  mtx -->|RTSP :8554| ts
  ds -->|RPC| carla
  ds -->|/detections/history| ts
  studio -->|WebSocket| ts
  browser -->|https://path2v2x.net| web
  browser -->|/ws| ds
  browser -->|/camera/ /archive/| mtx
  browser -->|/detections/| ts
  browser -->|https://twin.path2v2x.net| studio`;

	interface RepoRow {
		repo: string;
		url: string;
		owns: string;
		deploy: string;
	}

	const repos: RepoRow[] = [
		{
			repo: 'path2v2x/v2x-drive',
			url: 'https://github.com/path2v2x/v2x-drive',
			owns: 'This site (apps/drive-web), the CARLA drive server (apps/drive-server), the camera relay and 72 h recorder (scripts/ops/camera-relay), the path2v2x.net nginx vhost (scripts/ops/nginx), CARLA and drive systemd units.',
			deploy: 'scripts/deploy.sh [--server]'
		},
		{
			repo: 'path2v2x/v2x-digital-twin',
			url: 'https://github.com/path2v2x/v2x-digital-twin',
			owns: 'The twin server (apps/twin-server: SimForge world, ghosts from detections, 72 h detection history and replay), the twin.path2v2x.net vhost (deploy/nginx-twin.conf), the calibrated camera rig (config/drive-rigs), the perception service unit and config (scripts/systemd, config/perception), the Studio unit and env template (deploy/).',
			deploy: 'scripts/deploy.sh [--perception] [--studio]'
		},
		{
			repo: 'path2v2x/co-perception',
			url: 'https://github.com/path2v2x/co-perception',
			owns: 'The perception code itself: NVDEC ingest from the demux sockets, YOLOv8 + BoT-SORT, ground-plane projection to GPS, the /detections/latest endpoint. Deployed as a checkout that v2x-digital-twin points its service unit at. The camera demux pipeline on the RFS PC is run separately by its author.',
			deploy: 'v2x-digital-twin: scripts/deploy.sh --perception'
		},
		{
			repo: 'SimForgeinc/simforge-oss',
			url: 'https://github.com/SimForgeinc/simforge-oss',
			owns: 'The open-source simulation engine and Studio UI. The twin consumes its packages one-way (vendored into v2x-digital-twin) and runs its Studio Drive dashboard as the twin UI. Nothing project-specific lives here; behaviour is configured through environment variables and the twin protocol.',
			deploy: 'v2x-digital-twin: scripts/deploy.sh --studio'
		}
	];

	interface RouteRow {
		route: string;
		backend: string;
		repo: string;
	}

	const driveRoutes: RouteRow[] = [
		{ route: '/  /drive  /live  /timeline  /demo-videos  /architecture', backend: 'Static build in /var/www/v2x-drive (apps/drive-web); config.json ships with the build and is root-relative', repo: 'v2x-drive' },
		{ route: '/ws', backend: 'drive-server WebSocket, 127.0.0.1:8765 (CARLA drive sessions)', repo: 'v2x-drive' },
		{ route: '/data/api/state.json  /data/api/map-data.json  /data/snapshots/', backend: 'Files the drive server writes to /var/www/v2x-drive-data every few seconds', repo: 'v2x-drive' },
		{ route: '/data/demo-videos/', backend: 'Same directory, nginx JSON autoindex', repo: 'v2x-drive' },
		{ route: '/camera/chN/index.m3u8', backend: 'MediaMTX low-latency HLS, 127.0.0.1:8888', repo: 'v2x-drive' },
		{ route: '/archive/list  /archive/get', backend: 'MediaMTX playback server over the 72 h recordings, 127.0.0.1:9996', repo: 'v2x-drive' },
		{ route: '/detections/coverage  /objects  /history', backend: 'twin-server detection history, 127.0.0.1:8190 (the Timeline reads the twin\u2019s 72 h store)', repo: 'v2x-digital-twin' },
		{ route: '/perception  /perception/ws', backend: 'co-perception live viewer, 127.0.0.1:8766', repo: 'co-perception' }
	];

	const twinRoutes: RouteRow[] = [
		{ route: '/  (302)  /dashboard/drive  /_next/  /api/', backend: 'Studio Drive (next start), 127.0.0.1:5199', repo: 'simforge-oss' },
		{ route: '/twin  /drive  /camera-feeds', backend: 'twin-server WebSockets, 127.0.0.1:8865 (truth frames, drive commands, multiplexed camera JPEGs)', repo: 'v2x-digital-twin' },
		{ route: '/health  /streams/chN.mjpg  /detections/', backend: 'twin-server HTTP, 127.0.0.1:8190', repo: 'v2x-digital-twin' },
		{ route: '/drive-rigs/richmond.json', backend: 'Calibrated pole-camera rig served from config/drive-rigs (overrides the Studio fixture)', repo: 'v2x-digital-twin' },
		{ route: '/map/  /map-bundles/', backend: 'Richmond Field Station 3D bundle in /var/www/v2x-twin-map', repo: 'v2x-digital-twin' },
		{ route: '/camera/  /archive/', backend: 'Same MediaMTX relay as above (twin replay video)', repo: 'v2x-drive' }
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

			<!-- System diagram -->
			<section class="space-y-3">
				<h2 class="text-lg font-semibold text-white">How the pieces fit</h2>
				<p class="max-w-3xl text-sm text-gray-400">
					Boxes are processes on the RFS PC grouped by the repository that owns them. Only nginx listens on the
					internet; every arrow into the RFS PC is a path on one of the two hostnames, and every arrow inside it is a
					loopback port or a Unix socket.
				</p>
				<MermaidDiagram title="Repositories, processes, and routes" source={systemDiagram} />
			</section>

			<!-- Repositories -->
			<section class="space-y-3">
				<h2 class="text-lg font-semibold text-white">Repositories: what lives where</h2>
				<div class="overflow-x-auto rounded-3xl border border-gray-800 bg-gray-900/70">
					<table class="w-full text-left text-sm">
						<thead class="border-b border-gray-800 text-[11px] uppercase tracking-[0.16em] text-gray-500">
							<tr>
								<th class="px-4 py-3">Repository</th>
								<th class="px-4 py-3">Owns</th>
								<th class="px-4 py-3">Deploy</th>
							</tr>
						</thead>
						<tbody class="divide-y divide-gray-800/80 text-gray-300">
							{#each repos as row}
								<tr>
									<td class="px-4 py-2.5 align-top whitespace-nowrap">
										<a class="text-cyan-300 hover:underline" href={row.url} target="_blank" rel="noreferrer">{row.repo}</a>
									</td>
									<td class="px-4 py-2.5 align-top">{row.owns}</td>
									<td class="px-4 py-2.5 align-top font-mono text-xs whitespace-nowrap text-cyan-200/80">{row.deploy}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				<p class="text-xs text-gray-500">
					Rule of thumb: <span class="text-gray-300">v2x-drive</span> owns everything under path2v2x.net plus the shared camera relay;
					<span class="text-gray-300">v2x-digital-twin</span> owns everything under twin.path2v2x.net plus the detection history;
					<span class="text-gray-300">co-perception</span> is the only perception implementation; <span class="text-gray-300">simforge-oss</span> only supplies the twin engine and UI.
					The two cross-repo routes are <span class="font-mono text-cyan-200/80">/detections/</span> on the drive host and
					<span class="font-mono text-cyan-200/80">/camera/ /archive/</span> on the twin host; both are read-only proxies to loopback ports.
				</p>
			</section>

			<!-- Routes -->
			<section class="grid gap-6 lg:grid-cols-2">
				{#each [{ host: 'path2v2x.net', note: 'www redirects here; drive.path2v2x.net is an alias. Vhost: v2x-drive/scripts/ops/nginx/v2x-drive-public.conf', rows: driveRoutes }, { host: 'twin.path2v2x.net', note: 'Vhost: v2x-digital-twin/deploy/nginx-twin.conf', rows: twinRoutes }] as table}
					<div class="space-y-3">
						<div>
							<h2 class="text-lg font-semibold text-white">Routes on {table.host}</h2>
							<p class="text-xs text-gray-500">{table.note}</p>
						</div>
						<div class="overflow-x-auto rounded-3xl border border-gray-800 bg-gray-900/70">
							<table class="w-full text-left text-sm">
								<thead class="border-b border-gray-800 text-[11px] uppercase tracking-[0.16em] text-gray-500">
									<tr>
										<th class="px-4 py-3">Route</th>
										<th class="px-4 py-3">Backend</th>
										<th class="px-4 py-3">Repo</th>
									</tr>
								</thead>
								<tbody class="divide-y divide-gray-800/80 text-gray-300">
									{#each table.rows as row}
										<tr>
											<td class="px-4 py-2.5 align-top font-mono text-xs text-cyan-200/80">{row.route}</td>
											<td class="px-4 py-2.5 align-top">{row.backend}</td>
											<td class="px-4 py-2.5 align-top whitespace-nowrap text-white">{row.repo}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{/each}
			</section>

			<!-- Developer guide -->
			<section class="space-y-4 rounded-3xl border border-gray-800 bg-gray-900/70 p-5">
				<h2 class="text-lg font-semibold text-white">Developer guide</h2>
				<div class="grid gap-6 lg:grid-cols-2">
					<div class="space-y-3 text-sm text-gray-300">
						<h3 class="text-sm font-semibold text-white">Run this site locally</h3>
						<pre class="overflow-x-auto rounded-2xl border border-gray-800 bg-gray-950 px-4 py-3 font-mono text-xs text-cyan-100">git clone https://github.com/path2v2x/v2x-drive
cd v2x-drive/apps/drive-web
npm ci && npm run dev        # http://localhost:5173</pre>
						<p class="text-gray-400">
							The dev server proxies <span class="font-mono text-cyan-200/80">/ws /camera /archive /detections /data</span> to the
							production host, so <span class="font-mono text-cyan-200/80">static/config.json</span> stays root-relative everywhere.
							Set <span class="font-mono text-cyan-200/80">DRIVE_PROXY_TARGET</span> to point at another nginx.
						</p>
						<h3 class="pt-2 text-sm font-semibold text-white">Run the twin locally</h3>
						<pre class="overflow-x-auto rounded-2xl border border-gray-800 bg-gray-950 px-4 py-3 font-mono text-xs text-cyan-100">git clone https://github.com/path2v2x/v2x-digital-twin
cd v2x-digital-twin && pnpm install
pnpm --dir apps/twin-server start   # WS :8765, HTTP :8090
pnpm --dir apps/twin-server test</pre>
						<p class="text-gray-400">
							The Studio UI comes from a simforge-oss checkout: <span class="font-mono text-cyan-200/80">pnpm dev</span> there and
							set <span class="font-mono text-cyan-200/80">NEXT_PUBLIC_DRIVE_TWIN_URL</span> to your twin server.
						</p>
					</div>
					<div class="space-y-3 text-sm text-gray-300">
						<h3 class="text-sm font-semibold text-white">Deploy</h3>
						<pre class="overflow-x-auto rounded-2xl border border-gray-800 bg-gray-950 px-4 py-3 font-mono text-xs text-cyan-100"># push main first; each script fast-forwards the RFS PC checkout
v2x-drive/scripts/deploy.sh             # site + nginx
v2x-drive/scripts/deploy.sh --server    # + drive server (refuses mid-session)
v2x-digital-twin/scripts/deploy.sh      # twin server + units + nginx
v2x-digital-twin/scripts/deploy.sh --perception --studio</pre>
						<p class="text-gray-400">
							Scripts need Tailscale SSH to the RFS PC as root. They install the tracked units and nginx vhosts, restart the
							service, and fail on a bad health check. Rollback is a revert plus the same command.
						</p>
						<h3 class="pt-2 text-sm font-semibold text-white">Adding something</h3>
						<ul class="list-disc space-y-1.5 pl-5 text-gray-400">
							<li>A new page on this site: add a route under <span class="font-mono text-cyan-200/80">apps/drive-web/src/routes</span>; no server change.</li>
							<li>A new backend on path2v2x.net: bind it to loopback, add a <span class="font-mono text-cyan-200/80">location</span> to the tracked vhost, add a key to <span class="font-mono text-cyan-200/80">runtime-config.ts</span>, deploy.</li>
							<li>Something the twin needs: it belongs in v2x-digital-twin (server) or, if generic, as a PR to simforge-oss driven by env or the twin protocol.</li>
							<li>Perception changes go to co-perception; the service unit and pipeline config that run it live in v2x-digital-twin.</li>
							<li>Never edit files on the RFS PC by hand; everything under /etc and /var/www that matters is installed by a deploy script from a tracked file.</li>
						</ul>
						<h3 class="pt-2 text-sm font-semibold text-white">Where state lives</h3>
						<ul class="list-disc space-y-1.5 pl-5 text-gray-400">
							<li><span class="font-mono text-cyan-200/80">/mnt/archive/v2x-camera/recordings</span>: 72 h of video, guard timer keeps 60 GB free.</li>
							<li><span class="font-mono text-cyan-200/80">/var/lib/v2x-twin/detections.sqlite</span>: 72 h of detections.</li>
							<li><span class="font-mono text-cyan-200/80">/var/www/v2x-drive-data</span>: drive-server publications and demo videos.</li>
							<li>Nothing in the cloud except the Route 53 zone.</li>
						</ul>
					</div>
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
