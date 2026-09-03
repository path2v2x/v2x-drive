import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Power, PowerOff, Car, Trash2, ChevronRight, ChevronUp, ChevronDown, Circle,
  Play, Square, CircleDot, Plus, Save, FolderOpen, Gauge, ArrowUp, ArrowDown, ArrowLeft, ArrowRight,
  Pause, Download, Activity, Sun, Moon, Network,
} from 'lucide-react'
// Cockpit typography: B612 was designed for aircraft instrument displays
// (Airbus/ENAC) — the body & data faces of this console. Archivo carries the
// nameplate. Bundled locally by Parcel; no CDN dependency.
import '@fontsource/archivo/700.css'
import '@fontsource/archivo/800.css'
import '@fontsource/b612/400.css'
import '@fontsource/b612/700.css'
import '@fontsource/b612-mono/400.css'
import '@fontsource/b612-mono/700.css'

// ── Design tokens: "Prüfstand" — test-bench instrument, day/night dash ──
const CSS = `
.app{
  /* NIGHT (default): asphalt ground, amber dash illumination */
  --ground:#101317; --surface:#171C22; --inset:#0C0F13; --ink:#ECEAE4; --muted:#9AA3AE; --faint:#68737F;
  --line:#262D36; --line-2:#1E242C;
  --accent:#FFB300; --accent-ink:#1A1206; --accent-weak:rgba(255,179,0,.10); --accent-line:rgba(255,179,0,.38);
  --ok:#3FBF7F; --ok-weak:rgba(63,191,127,.12); --warn:#E0A100; --warn-weak:rgba(224,161,0,.12);
  --crit:#E5484D; --crit-weak:rgba(229,72,77,.12); --rec:#FF4D6D; --rec-weak:rgba(255,77,109,.12);
  --lampglow:0 0 9px; --bezel:inset 0 0 0 1px rgba(255,255,255,.04), inset 0 14px 34px rgba(0,0,0,.5);
  --display:'Archivo',system-ui,sans-serif;
  --sans:'B612',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'B612 Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
}
.app.day{
  /* DAY: warm concrete, ink, amber deepened for contrast */
  --ground:#E9E7E1; --surface:#F7F6F2; --inset:#EFEDE6; --ink:#16181B; --muted:#565E68; --faint:#8A9199;
  --line:#CFCCC3; --line-2:#DEDBD3;
  --accent:#A66A00; --accent-ink:#FFF7E6; --accent-weak:rgba(166,106,0,.10); --accent-line:rgba(166,106,0,.42);
  --ok:#0F7B43; --ok-weak:rgba(15,123,67,.10); --warn:#8A6400; --warn-weak:rgba(138,100,0,.12);
  --crit:#B02A21; --crit-weak:rgba(176,42,33,.09); --rec:#C21F4E; --rec-weak:rgba(194,31,78,.10);
  --lampglow:0 0 0px; --bezel:inset 0 0 0 1px rgba(0,0,0,.05), inset 0 10px 26px rgba(0,0,0,.18);
}
*{box-sizing:border-box}
.app{min-height:100vh;background:var(--ground);color:var(--ink);font-family:var(--sans);padding:20px;line-height:1.5;-webkit-font-smoothing:antialiased;transition:background .25s,color .25s}
.wrap{max-width:1240px;margin:0 auto}
.mono{font-family:var(--mono)} .tnum{font-variant-numeric:tabular-nums}
.eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.spring{flex:1 1 40px}

/* ── nameplate masthead ── */
.plate{display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:13px 16px;position:relative;overflow:hidden}
.plate::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:linear-gradient(90deg,var(--accent) 0 72px,var(--line) 72px 100%)}
.plate-id{display:flex;align-items:center;gap:12px}
.plate-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:5px;background:var(--accent);color:var(--accent-ink)}
.plate-model{font-family:var(--display);font-size:19px;font-weight:800;letter-spacing:.02em;line-height:1.1;text-wrap:balance}
.plate-model span{color:var(--faint);font-weight:700}
.plate-sub{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-top:3px}
.lamps{display:flex;gap:7px}
.lamp{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9.5px;font-weight:700;letter-spacing:.12em;padding:4px 9px;border:1px solid var(--line);border-radius:4px;color:var(--faint);background:var(--inset);user-select:none}
.lamp::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--line)}
.lamp.on{color:var(--ink);border-color:var(--accent-line)}
.lamp.on::before{background:var(--accent);box-shadow:var(--lampglow) var(--accent)}
.lamp.ok.on::before{background:var(--ok);box-shadow:var(--lampglow) var(--ok)}
.lamp.rec.on{color:var(--rec)} .lamp.rec.on::before{background:var(--rec);box-shadow:var(--lampglow) var(--rec)}
@media (prefers-reduced-motion:no-preference){.lamp.rec.on::before{animation:pulse 1s infinite}}
.modesw{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:7px 11px;border:1px solid var(--line);border-radius:4px;background:var(--inset);color:var(--muted);cursor:pointer}
.modesw:hover{color:var(--ink);border-color:var(--accent-line)}
.modesw:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
a.modesw{text-decoration:none}

/* ── bench (endpoint) row ── */
.bench{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:12px}
.endpoint{display:flex;gap:8px;flex:2 1 460px;min-width:300px}
.pill{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:6px 11px;border-radius:4px;border:1px solid var(--line);background:var(--surface)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint)}
.st-open .dot{background:var(--ok);box-shadow:var(--lampglow) var(--ok)} .st-open{color:var(--ok);border-color:var(--ok);background:var(--ok-weak)}
.st-closed .dot{background:var(--crit)} .st-closed{color:var(--crit);border-color:var(--crit);background:var(--crit-weak)}
.st-connecting .dot{background:var(--warn)} .st-connecting{color:var(--warn);border-color:var(--warn);background:var(--warn-weak)}
@media (prefers-reduced-motion:no-preference){.st-connecting .dot{animation:pulse 1s infinite}}
.st-disconnected{color:var(--muted)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.field{font-family:var(--mono);font-size:13px;padding:8px 11px;border:1px solid var(--line);border-radius:4px;background:var(--inset);color:var(--ink);width:100%}
.field::placeholder{color:var(--faint)}
.field:focus-visible{outline:2px solid var(--accent);outline-offset:-1px;border-color:var(--accent)}
.btn{font-family:var(--sans);font-size:12.5px;font-weight:700;display:inline-flex;align-items:center;gap:6px;padding:8px 13px;border-radius:4px;border:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s,transform .04s;user-select:none}
.btn:hover:not(:disabled){border-color:var(--accent-line);background:var(--accent-weak)}
.btn:active:not(:disabled){transform:translateY(1px)}
.btn:disabled{opacity:.4;cursor:not-allowed} .btn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn-accent{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn-accent:hover:not(:disabled){background:var(--accent);filter:brightness(1.08)}
.btn-ok{color:var(--ok);border-color:var(--ok)} .btn-ok:hover:not(:disabled){background:var(--ok-weak);border-color:var(--ok)}
.btn-crit{color:var(--crit);border-color:var(--crit)} .btn-crit:hover:not(:disabled){background:var(--crit-weak);border-color:var(--crit)}
.btn-rec{color:var(--rec);border-color:var(--rec)} .btn-rec:hover:not(:disabled){background:var(--rec-weak);border-color:var(--rec)}
.btn-sm{padding:5px 9px;font-size:11.5px}
.icon{width:15px;height:15px;flex:none} .icon-sm{width:13px;height:13px;flex:none}

/* ── instrument housings ── */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.panel-hd{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 13px;border-bottom:1px solid var(--line-2);background:linear-gradient(180deg,var(--surface),var(--surface)),var(--surface)}
.panel-bd{padding:13px}
/* live view + injector share one row and one height: the row is a grid with
   stretch alignment, both children are flex columns, and the injector's
   textarea absorbs the leftover height. The stage's 1:1 aspect sets the pace. */
.grid2{display:grid;grid-template-columns:1.15fr 1fr;gap:14px;align-items:stretch;margin-top:14px}
.grid2>.panel{display:flex;flex-direction:column}
.pfill{flex:1;display:flex;flex-direction:column;min-height:0}
.injfill{flex:1;min-height:120px;resize:none}
@media (max-width:920px){.grid2{grid-template-columns:1fr}}
/* full-width script deck: capture rig left, sequence builder right */
.deck{display:grid;grid-template-columns:minmax(260px,320px) 1fr;gap:20px;align-items:start}
@media (max-width:920px){.deck{grid-template-columns:1fr}}
.sess-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;padding:10px 13px;background:var(--surface);border:1px solid var(--line);border-radius:6px}
.kv{display:flex;flex-direction:column;gap:1px}
.kv .k{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
.kv .v{font-family:var(--mono);font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
.sep{width:1px;align-self:stretch;background:var(--line)}
.views{display:inline-flex;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.views button{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:6px 11px;border:none;background:var(--inset);color:var(--muted);cursor:pointer;border-left:1px solid var(--line)}
.views button:first-child{border-left:none}
.views button.on{background:var(--accent);color:var(--accent-ink);font-weight:700}
.views button:disabled{opacity:.4;cursor:not-allowed}
.views button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}

/* ── stage (camera bezel) + telemetry cluster ── */
.stage{position:relative;background:#08090C;border-radius:5px;overflow:hidden;aspect-ratio:1/1;display:grid;place-items:center;box-shadow:var(--bezel)}
.stage img{width:100%;height:100%;object-fit:cover;display:block}
.stage .placeholder{color:#5F6873;font-family:var(--mono);font-size:12px;text-align:center;padding:20px;position:relative}
.hud{display:grid;grid-template-columns:auto 1fr;gap:9px 14px;align-items:center;margin-top:12px}
.speedo{grid-row:span 3;text-align:center;padding:6px 16px 6px 4px;border-right:1px solid var(--line)}
.speedo .n{font-family:var(--mono);font-size:36px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.speedo .u{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-top:3px}
.meter{display:grid;grid-template-columns:58px 1fr 44px;gap:8px;align-items:center;font-family:var(--mono);font-size:11px}
.meter .lab{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:9.5px}
.track{height:8px;background:var(--inset);border:1px solid var(--line);border-radius:2px;overflow:hidden;position:relative}
.fill{height:100%}
.meter .val{text-align:right;color:var(--ink);font-variant-numeric:tabular-nums}
.steerwrap{position:relative} .steerwrap .mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--faint)}

/* ── script deck ── */
.drivepad{display:grid;grid-template-columns:repeat(3,58px);grid-template-rows:repeat(2,auto);gap:6px;flex:0 0 auto}
.pad{font-family:var(--mono);font-size:12px;font-weight:700;display:flex;flex-direction:column;align-items:center;gap:3px;padding:11px 6px;border:1px solid var(--line);border-radius:4px;background:var(--inset);color:var(--ink);cursor:pointer;user-select:none;touch-action:none}
.pad:disabled{opacity:.4;cursor:not-allowed}
.pad.act{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
/* classic WASD "T": W top-centre, A S D across the bottom */
.pad.gas{grid-column:2;grid-row:1} .pad.a{grid-column:1;grid-row:2} .pad.brk{grid-column:2;grid-row:2} .pad.d{grid-column:3;grid-row:2}
.pad:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
/* small segmented view toggle (reuses .views look) */
.views.vsm button{padding:4px 9px;font-size:10px}
/* raw packet injector — the primary low-level tool */
.inj{font-family:var(--mono);font-size:12px;line-height:1.5;min-height:76px;resize:vertical;white-space:pre;width:100%;
  padding:9px 11px;border:1px solid var(--line);border-radius:4px;background:#08090C;color:#E3C98B;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)}
.inj::placeholder{color:#565F6B}
.inj:focus-visible{outline:2px solid var(--accent);outline-offset:-1px;border-color:var(--accent)}
.injrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.injnote{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
/* wire-truth view of the script — styled like the flight recorder */
.pktscript{background:#08090C;border:1px solid var(--line);border-radius:4px;padding:8px 11px;font-family:var(--mono);font-size:11.5px;line-height:1.6;overflow-x:auto;margin-top:6px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)}
.pktline{padding:4px 0;border-top:1px dashed rgba(255,255,255,.07)}
.pktline:first-child{border-top:none}
.pktline .c{color:#565F6B;display:block;font-size:10.5px;white-space:nowrap}
.pktline code{color:#E3C98B;white-space:pre}
.pktline.on .c{color:var(--faint)} .pktline.on code{color:var(--accent)}
.pktfoot{color:#565F6B;font-size:10.5px;padding-top:6px;border-top:1px dashed rgba(255,255,255,.07);margin-top:2px}
.hint{font-size:11px;color:var(--faint);font-family:var(--mono);margin-top:8px}
.steps{margin-top:6px;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.step{display:grid;grid-template-columns:24px 1fr auto auto;gap:9px;align-items:center;padding:6px 9px;border-top:1px solid var(--line-2);font-family:var(--mono);font-size:12px;background:var(--inset)}
.step:first-child{border-top:none}
.step.on{background:var(--accent-weak);box-shadow:inset 2px 0 0 var(--accent)}
.step .ix{color:var(--faint);text-align:right;font-variant-numeric:tabular-nums}
.step .lb{font-weight:700}
.step .du{color:var(--muted);font-variant-numeric:tabular-nums}
.step .ops{display:flex;gap:3px}
.iconbtn{border:1px solid var(--line);background:var(--surface);border-radius:3px;padding:3px;cursor:pointer;color:var(--muted);display:grid;place-items:center}
.iconbtn:hover:not(:disabled){border-color:var(--accent-line);color:var(--ink)} .iconbtn:disabled{opacity:.35;cursor:not-allowed}
.iconbtn:focus-visible{outline:2px solid var(--accent)}
.addrow{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:10px}
.select{font-family:var(--mono);font-size:12px;padding:7px 9px;border:1px solid var(--line);border-radius:4px;background:var(--inset);color:var(--ink)}
.select:focus-visible{outline:2px solid var(--accent)}
.dur{width:74px}
.empty{padding:14px;text-align:center;font-family:var(--mono);font-size:12px;color:var(--faint);font-style:italic;background:var(--inset)}
.savedrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:11px;padding-top:11px;border-top:1px solid var(--line-2)}
.chip{font-family:var(--mono);font-size:11px;padding:4px 9px;border-radius:3px;border:1px solid var(--line);background:var(--inset);display:inline-flex;align-items:center;gap:6px}
.chip button{border:none;background:none;cursor:pointer;color:var(--faint);padding:0;display:grid;place-items:center}
.recdot{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;color:var(--rec);font-weight:700;letter-spacing:.1em}
.recdot .d{width:8px;height:8px;border-radius:50%;background:var(--rec);box-shadow:var(--lampglow) var(--rec)}
@media (prefers-reduced-motion:no-preference){.recdot .d{animation:pulse 1s infinite}}

/* ── datasheet (API reference) ── */
.docs{margin-top:14px}
.doc{border:1px solid var(--line);border-radius:5px;background:var(--surface);overflow:hidden;margin-top:8px}
.doc>summary{list-style:none;cursor:pointer;padding:10px 14px;display:flex;align-items:center;gap:10px;font-family:var(--mono);font-size:13px;font-weight:700}
.doc>summary::-webkit-details-marker{display:none}
.doc>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.doc>summary .secno{font-family:var(--mono);font-size:10.5px;color:var(--faint);font-variant-numeric:tabular-nums;letter-spacing:.05em}
.doc>summary .tag{font-family:var(--mono);font-size:9.5px;font-weight:700;letter-spacing:.08em;padding:2px 7px;border-radius:3px;border:1px solid var(--accent-line);background:var(--accent-weak);color:var(--accent)}
.doc>summary .tag.resp{border-color:var(--ok);background:var(--ok-weak);color:var(--ok)}
.app.day .doc>summary .tag{color:#7A4E00}
.doc>summary .chev{margin-left:auto;color:var(--faint);transition:transform .12s}
.doc[open]>summary .chev{transform:rotate(90deg)}
.doc .body{padding:0 14px 14px;border-top:1px solid var(--line-2)}
.doc p{font-size:13px;color:var(--muted);margin:11px 0 9px;max-width:72ch}
.doc pre{background:var(--inset);border:1px solid var(--line);border-radius:4px;padding:10px 12px;overflow-x:auto;font-family:var(--mono);font-size:12px;color:var(--ink);margin:0 0 10px}
.doc table{width:100%;border-collapse:collapse;font-size:12px;font-family:var(--mono)}
.doc th,.doc td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line-2);vertical-align:top}
.doc th{color:var(--faint);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:700}
.doc td code{background:var(--inset);border:1px solid var(--line-2);border-radius:2px;padding:1px 4px}
.callout{background:var(--accent-weak);border:1px solid var(--accent-line);border-radius:4px;padding:10px 13px;font-size:12.5px;color:var(--ink);margin-top:6px}
.err{margin-top:10px;padding:8px 12px;border-radius:4px;background:var(--crit-weak);border:1px solid var(--crit);color:var(--crit);font-family:var(--mono);font-size:12px}
.h2{font-family:var(--display);font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ink);margin:24px 0 2px;display:flex;align-items:center;gap:10px}
.h2::after{content:"";flex:1;height:1px;background:var(--line)}

/* ── flight recorder (wire log) — the black box stays black in both modes ── */
.wire-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.wcheck{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:11px;color:var(--muted);cursor:pointer;user-select:none}
.wcheck input{accent-color:var(--accent);cursor:pointer}
.wire-stream{height:300px;overflow:auto;background:#08090C;border:1px solid var(--line);border-radius:4px;padding:4px 0;
  font-family:var(--mono);font-size:11.5px;line-height:1.55;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)}
.wrow{display:grid;grid-template-columns:86px 30px 1fr;gap:9px;align-items:baseline;padding:1px 10px;white-space:pre-wrap;word-break:break-all}
.wrow:hover{background:#14171D}
.wt{color:#565F6B;font-variant-numeric:tabular-nums;font-size:10.5px;white-space:nowrap}
.wd{font-weight:700;font-size:10px;letter-spacing:.05em}
.wrow.tx .wd{color:#FFB300} .wrow.tx .wp{color:#E3C98B}
.wrow.rx .wd{color:#7FB8D8} .wrow.rx .wp{color:#AECBDD}
.wrow.bin .wd{color:#7A828D} .wrow.bin .wp{color:#5E6771;font-style:italic}
.wrow.evt .wd{color:#EDEDED} .wrow.evt .wp{color:#B8BDC4;font-style:italic}
.wire-empty{padding:12px;color:#565F6B;font-style:italic;font-family:var(--mono);font-size:12px}
.wire-count{font-family:var(--mono);font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
`

// ── Helpers / model ──────────────────────────────────────────────────
type Status = 'disconnected' | 'connecting' | 'open' | 'closed'
type Ctrl = { s: number; t: number; b: number; rev?: boolean }
type Step = { id: number; s: number; t: number; b: number; rev?: boolean; ms: number; label: string }
type Telem = { speed: number; gear: number; pos: number[]; steer: number; throttle: number; brake: number }
type Saved = { name: string; steps: Step[] }
// Wire log — every frame on the socket, kept verbatim
type WireDir = 'TX' | 'RX' | 'EVT'
type WirePkt = { i: number; t: string; dir: WireDir; kind: 'json' | 'bin' | 'evt'; ptype: string; data: string; bytes?: number }
const WIRE_CAP = 3000
const wireTime = () => {
  const d = new Date()
  return d.toLocaleTimeString([], { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

const isoWindow = () => {
  const now = Date.now()
  return { start: new Date(now - 3600_000).toISOString(), end: new Date(now).toISOString() }
}
const ctrlLabel = (c: Ctrl) => {
  const p: string[] = []
  if (c.t > 0) p.push(c.rev ? 'Reverse' : 'Gas'); if (c.b > 0) p.push('Brake')
  if (c.s < 0) p.push('Left'); if (c.s > 0) p.push('Right')
  return p.join(' + ') || 'Coast'
}
const PRESETS: { label: string; s: number; t: number; b: number; rev?: boolean }[] = [
  { label: 'Gas', s: 0, t: 1, b: 0 },
  { label: 'Reverse', s: 0, t: 0.6, b: 0, rev: true },
  { label: 'Brake', s: 0, t: 0, b: 1 },
  { label: 'Coast', s: 0, t: 0, b: 0 },
  { label: 'Left + Gas', s: -0.7, t: 0.6, b: 0 },
  { label: 'Right + Gas', s: 0.7, t: 0.6, b: 0 },
  { label: 'Hard Left', s: -1, t: 0.3, b: 0 },
  { label: 'Hard Right', s: 1, t: 0.3, b: 0 },
]
const VIEWS = ['chase', 'hood', 'bird', 'free']
const LS_KEY = 'carla-dev-scripts'
let SID = 1

const DOCS = [
  {
    dir: 'send', name: 'start_session', title: 'Spawn a car & begin streaming',
    body: (
      <>
        <p>Spawns your ego vehicle at a valid map spawn point, reconstructs the V2X scene for the given time window, attaches a camera, and begins the MJPEG frame stream. <code>start</code>/<code>end</code> are ISO-8601 timestamps defining the V2X reconstruction window (not a location).</p>
        <pre>{`{ "type": "start_session",
  "start": "2026-07-09T09:00:00Z",   // ISO time window begin
  "end":   "2026-07-09T10:00:00Z",   // ISO time window end
  "vehicle": "vehicle.tesla.model3" } // blueprint id`}</pre>
        <div className="callout">Frames don't flow until a <code>camera_switch</code> is issued after spawn — the dev console sends one automatically.</div>
      </>
    ),
  },
  {
    dir: 'send', name: 'control', title: 'Drive the car',
    body: (
      <>
        <p>The core driving packet. Applied to the car bound to <em>this connection</em>. Send it continuously (≈20 Hz) to hold an input, exactly like a driver holding a pedal. Values are clamped server-side.</p>
        <pre>{`{ "type": "control",
  "s": -1.0,   // steer   -1.0 (full left) … +1.0 (full right)
  "t":  1.0,   // throttle 0.0 … 1.0
  "b":  0.0,   // brake    0.0 … 1.0
  "rev": false // reverse gear
}`}</pre>
        <table><thead><tr><th>Field</th><th>Meaning</th><th>Range</th></tr></thead><tbody>
          <tr><td><code>s</code></td><td>steer</td><td>-1.0 … 1.0</td></tr>
          <tr><td><code>t</code></td><td>throttle</td><td>0.0 … 1.0</td></tr>
          <tr><td><code>b</code></td><td>brake</td><td>0.0 … 1.0</td></tr>
          <tr><td><code>rev</code></td><td>reverse</td><td>boolean</td></tr>
        </tbody></table>
      </>
    ),
  },
  {
    dir: 'send', name: 'camera_switch', title: 'Change camera view',
    body: (<><p>Switches the streamed camera. Views: <code>chase</code>, <code>hood</code>, <code>bird</code>, <code>free</code>.</p><pre>{`{ "type": "camera_switch", "view": "chase" }`}</pre></>),
  },
  {
    dir: 'send', name: 'respawn', title: 'Reset position',
    body: (<><p>Teleports the car back to a spawn point (velocity zeroed). Useful between script runs.</p><pre>{`{ "type": "respawn" }`}</pre></>),
  },
  {
    dir: 'send', name: 'end_session', title: 'Despawn & end',
    body: (<><p>Despawns the car and camera, ends the session. The connection stays open — you can <code>start_session</code> again.</p><pre>{`{ "type": "end_session" }`}</pre></>),
  },
  {
    dir: 'send', name: 'server_status', title: 'Query server',
    body: (<><p>Returns active session count, whether this connection has a session, and the current map.</p><pre>{`{ "type": "server_status" }`}</pre></>),
  },
  {
    dir: 'resp', name: 'session_ready', title: 'Response · spawn confirmed',
    body: (<><p>Sent after <code>start_session</code> succeeds. <code>vehicle_id</code> is the CARLA actor id of <em>your</em> car — the client-visible identity for this session.</p><pre>{`{ "type": "session_ready", "vehicle_id": 35, "objects_count": 0 }`}</pre></>),
  },
  {
    dir: 'resp', name: 'telemetry', title: 'Response · vehicle state',
    body: (<><p>Returned in response to <code>control</code> (and on tick). Speed is m/s.</p><pre>{`{ "type": "telemetry",
  "speed": 3.5, "gear": 1,
  "pos": [78.9, -91.1, 10.7], "rot": [0, 182, 0],
  "steer": -1.0, "throttle": 1.0, "brake": 0.0 }`}</pre></>),
  },
  {
    dir: 'resp', name: 'binary frame', title: 'Response · camera (binary)',
    body: (<><p>Camera frames arrive as <strong>binary</strong> WebSocket messages — raw JPEG bytes, ~10–20 fps. Render each directly (e.g. an object URL on an <code>&lt;img&gt;</code>). All non-binary messages are JSON.</p></>),
  },
]

// Static docs — memoized so the 20 Hz telemetry stream never re-renders it
const DocList = memo(function DocList() {
  return (
    <>
      <div className="h2">Datasheet — message reference</div>
      <p style={{ fontSize: 13, color: 'var(--muted)', margin: '6px 0 0', maxWidth: '78ch' }}>
        The drive server speaks JSON over one WebSocket. <strong>Identity is the connection</strong> — each connection gets its own <code className="mono">DriveSession</code> and car (server-side role <code className="mono">ego_vehicle_&lt;hex&gt;</code>); the client-visible id is <code className="mono">vehicle_id</code> from <code className="mono">session_ready</code>. Two clients = two connections = two cars in one world. <a href="./diagram.html" target="_blank" rel="noopener" style={{ color: 'var(--accent)' }}>See the interactive routing diagram →</a>
      </p>
      <div className="docs">
        {DOCS.map((d, i) => (
          <details className="doc" key={d.name} open={d.name === 'control'}>
            <summary>
              <span className="secno">§{String(i + 1).padStart(2, '0')}</span>
              <span className={`tag ${d.dir === 'resp' ? 'resp' : ''}`}>{d.dir === 'resp' ? 'SERVER→' : 'CLIENT→'}</span>
              <span className="mono">{d.name}</span>
              <span style={{ color: 'var(--faint)', fontWeight: 400, fontSize: 12 }}>{d.title}</span>
              <ChevronRight className="icon chev" />
            </summary>
            <div className="body">{d.body}</div>
          </details>
        ))}
      </div>
    </>
  )
})

// ── App ──────────────────────────────────────────────────────────────
export default function App() {
  const [url, setUrl] = useState('ws://localhost:8765/')
  const [status, setStatus] = useState<Status>('disconnected')
  const [vehicles, setVehicles] = useState<string[]>(['vehicle.lincoln.mkz'])
  const [vehicle, setVehicle] = useState('vehicle.lincoln.mkz')
  const [sessionActive, setSessionActive] = useState(false)
  const [vehicleId, setVehicleId] = useState<number | null>(null)
  const [view, setView] = useState('chase')
  const [telem, setTelem] = useState<Telem | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [spawning, setSpawning] = useState(false)

  const [steps, setSteps] = useState<Step[]>([])
  const [presetIdx, setPresetIdx] = useState(0)
  const [dur, setDur] = useState('2.0')
  const [scriptView, setScriptView] = useState<'steps' | 'packets'>('steps')
  const [copiedPkts, setCopiedPkts] = useState(false)
  const [inj, setInj] = useState('{ "type": "server_status" }')
  const [sessionInfo, setSessionInfo] = useState<any>(null)
  // Operating mode. ASSISTED: the console transmits for you (20 Hz control
  // loop, pads drive, script runner sends). RAW: the console never sends a
  // control packet on its own — the injector is the only TX path; pads and
  // capture only AUTHOR packets.
  const [opMode, setOpMode] = useState<'assisted' | 'raw'>('assisted')
  const opModeRef = useRef(opMode)
  useEffect(() => { opModeRef.current = opMode }, [opMode])
  // Gear picker: D = forward, R = reverse. Throttle inputs carry rev:true
  // while in R — the packet-level truth behind a real gear selector.
  const [gear, setGear] = useState<'D' | 'R'>('D')
  const gearRef = useRef(gear)
  useEffect(() => { gearRef.current = gear }, [gear])
  const inReverse = () => gearRef.current === 'R'
  const idleCtrl = (): Ctrl => ({ s: 0, t: 0, b: 0, rev: inReverse() })
  const pickGear = (g: 'D' | 'R') => {
    setGear(g); gearRef.current = g
    active.current = { ...active.current, rev: g === 'R' }
  }
  useEffect(() => {
    try { const s = localStorage.getItem('carla-dev-opmode'); if (s === 'raw' || s === 'assisted') setOpMode(s) } catch { /* ignore */ }
  }, [])
  const [playingId, setPlayingId] = useState<number | null>(null)
  const [mode, setMode] = useState<'idle' | 'manual' | 'playing'>('idle')
  const [manual, setManual] = useState(false)
  const [recording, setRecording] = useState(false)
  const [saved, setSaved] = useState<Saved[]>([])
  const [scriptName, setScriptName] = useState('')

  const wsRef = useRef<WebSocket | null>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const lastUrl = useRef<string | null>(null)
  const active = useRef<Ctrl>({ s: 0, t: 0, b: 0 })
  const keys = useRef<Set<string>>(new Set())
  const rec = useRef<{ on: boolean; segs: Step[]; last: Ctrl; t: number }>({ on: false, segs: [], last: { s: 0, t: 0, b: 0 }, t: 0 })
  const abort = useRef(false)
  const lastTelemT = useRef(0)   // throttle HUD re-renders to ~8 Hz
  const lastFrameT = useRef(0)   // stall detection
  const userClose = useRef(false) // distinguish user disconnect from a drop
  const [stalled, setStalled] = useState(false)
  const [streaming, setStreaming] = useState(false) // first frame arrived
  const streamingRef = useRef(false)

  // ── Wire log: every socket frame, verbatim ──
  const wireSeq = useRef(0)
  const wireBuf = useRef<WirePkt[]>([])
  const wireDirty = useRef(false)
  const wireTotal = useRef(0)
  const [wire, setWire] = useState<WirePkt[]>([])
  const [wirePaused, setWirePaused] = useState(false)
  const [wireFilters, setWireFilters] = useState({ control: true, telemetry: true, binary: true, other: true })

  const recordWire = useCallback((dir: WireDir, kind: WirePkt['kind'], ptype: string, data: string, bytes?: number) => {
    wireTotal.current++
    wireBuf.current.push({ i: wireSeq.current++, t: wireTime(), dir, kind, ptype, data, bytes })
    if (wireBuf.current.length > WIRE_CAP) {
      // Evict oldest high-frequency entry first so one-shot commands
      // (start_session, session_ready, teleport…) survive the flood.
      const evictIdx = wireBuf.current.findIndex(
        (p) => p.ptype === 'control' || p.ptype === 'telemetry' || p.kind === 'bin'
      )
      wireBuf.current.splice(evictIdx === -1 ? 0 : evictIdx, 1)
    }
    wireDirty.current = true
  }, [])

  // Batch UI updates (~3/s) so 40 pkt/s doesn't melt the render loop
  useEffect(() => {
    const id = window.setInterval(() => {
      if (wirePaused || !wireDirty.current) return
      wireDirty.current = false
      setWire([...wireBuf.current])
    }, 300)
    return () => window.clearInterval(id)
  }, [wirePaused])

  const wireShown = useMemo(() => {
    const cls = (p: WirePkt) =>
      p.kind === 'bin' ? 'binary' : p.ptype === 'control' ? 'control' : p.ptype === 'telemetry' ? 'telemetry' : 'other'
    return wire.filter((p) => wireFilters[cls(p) as keyof typeof wireFilters]).slice(-500)
  }, [wire, wireFilters])

  const wireDownload = () => {
    const lines = wireBuf.current.map((p) =>
      JSON.stringify({ t: p.t, dir: p.dir, kind: p.kind, type: p.ptype, ...(p.bytes !== undefined ? { bytes: p.bytes } : {}), raw: p.data })
    )
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'application/x-ndjson' })
    const u = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = u; a.download = 'wire-log.jsonl'; a.click()
    window.setTimeout(() => URL.revokeObjectURL(u), 5000)
  }
  const wireClear = () => { wireBuf.current = []; wireTotal.current = 0; wireSeq.current = 0; setWire([]) }

  const open = status === 'open'

  useEffect(() => {
    const local = /^(localhost|127\.|10\.|192\.168\.|100\.|path-b860i)/.test(location.hostname)
    setUrl(local ? `ws://${location.hostname}:8765/` : 'wss://path2v2x.net/ws')
    try { setSaved(JSON.parse(localStorage.getItem(LS_KEY) || '[]')) } catch { /* ignore */ }
  }, [])

  const sendCtrl = (c: Ctrl) => {
    const ws = wsRef.current
    if (ws && ws.readyState === 1) {
      const raw = JSON.stringify({ type: 'control', s: c.s, t: c.t, b: c.b, rev: c.rev ?? false })
      ws.send(raw)
      recordWire('TX', 'json', 'control', raw)
    }
  }
  const sendJSON = (o: any) => {
    const ws = wsRef.current
    if (ws && ws.readyState === 1) {
      const raw = JSON.stringify(o)
      ws.send(raw)
      recordWire('TX', 'json', o?.type ?? '?', raw)
    }
  }

  // Switching to RAW must silence every automatic TX path immediately:
  // stop a playing script, release manual driving, zero the held input.
  const switchOpMode = (m: 'assisted' | 'raw') => {
    setOpMode(m)
    try { localStorage.setItem('carla-dev-opmode', m) } catch { /* ignore */ }
    if (m === 'raw') {
      abort.current = true; setPlayingId(null); setMode('idle')
      setManual(false); active.current = idleCtrl()
    }
  }

  // Raw injector: whatever is in the box goes down the socket verbatim —
  // no validation, no parsing gate. The server's reaction (or silence) is
  // the lesson; watch the flight recorder.
  const injectRaw = () => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== 1 || !inj.length) return
    ws.send(inj)
    let ptype = 'raw'
    try { ptype = JSON.parse(inj)?.type ?? 'json (no type)' } catch { ptype = 'raw (not JSON)' }
    recordWire('TX', 'json', ptype, inj)
  }

  // 20 Hz loop while a session is live. In ASSISTED mode it transmits the
  // held input as control packets. In RAW mode it transmits NOTHING — it
  // only keeps servicing the recorder so live capture can still author
  // steps (which you then send yourself through the injector).
  useEffect(() => {
    if (!sessionActive) return
    const id = window.setInterval(() => {
      const c = active.current
      if (opModeRef.current === 'assisted') sendCtrl(c)
      const r = rec.current
      if (r.on && (c.s !== r.last.s || c.t !== r.last.t || c.b !== r.last.b || (c.rev ?? false) !== (r.last.rev ?? false))) {
        r.segs.push({ id: SID++, ...r.last, ms: Date.now() - r.t, label: ctrlLabel(r.last) })
        r.last = { ...c }; r.t = Date.now()
      }
    }, 50)
    return () => window.clearInterval(id)
  }, [sessionActive])

  // stall detection: flag if frames stop arriving while a session is active
  useEffect(() => {
    if (!sessionActive) { setStalled(false); return }
    const id = window.setInterval(() => {
      if (lastFrameT.current && Date.now() - lastFrameT.current > 3000) setStalled(true)
    }, 1000)
    return () => window.clearInterval(id)
  }, [sessionActive])

  // keyboard driving (only in manual mode)
  useEffect(() => {
    if (!manual || !sessionActive) { keys.current.clear(); if (mode === 'manual') { active.current = idleCtrl(); setMode('idle') } return }
    const recompute = () => {
      const k = keys.current
      const s = (k.has('a') ? -0.7 : 0) + (k.has('d') ? 0.7 : 0)
      const t = k.has('w') ? 1 : 0
      const b = (k.has('s') || k.has(' ')) ? 1 : 0
      active.current = { s, t, b, rev: inReverse() }; setMode('manual')
    }
    const kd = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (el && /^(input|textarea|select)$/i.test(el.tagName)) return // don't hijack typing in fields
      const k = e.key.toLowerCase(); if ('wasd '.includes(k)) { e.preventDefault(); keys.current.add(k); recompute() }
    }
    const ku = (e: KeyboardEvent) => { const k = e.key.toLowerCase(); if (keys.current.delete(k)) recompute() }
    window.addEventListener('keydown', kd); window.addEventListener('keyup', ku)
    return () => { window.removeEventListener('keydown', kd); window.removeEventListener('keyup', ku) }
  }, [manual, sessionActive, mode])

  const handleMsg = useCallback((ev: MessageEvent) => {
    if (ev.data instanceof Blob) {
      recordWire('RX', 'bin', 'frame', '[binary JPEG camera frame]', ev.data.size)
      lastFrameT.current = Date.now()
      setStalled(false)
      if (!streamingRef.current) { streamingRef.current = true; setStreaming(true) }
      const u = URL.createObjectURL(ev.data)
      if (imgRef.current) imgRef.current.src = u
      if (lastUrl.current) URL.revokeObjectURL(lastUrl.current)
      lastUrl.current = u
      return
    }
    let m: any
    try { m = JSON.parse(ev.data) } catch { recordWire('RX', 'json', 'unparsed', String(ev.data)); return }
    recordWire('RX', 'json', m?.type ?? '?', ev.data)
    switch (m.type) {
      case 'session_ready': setVehicleId(m.vehicle_id); setSessionInfo(m); setSessionActive(true); setSpawning(false); setErr(null); lastFrameT.current = Date.now(); setTimeout(() => sendJSON({ type: 'camera_switch', view }), 300); break
      case 'telemetry': { const now = Date.now(); if (now - lastTelemT.current >= 120) { lastTelemT.current = now; setTelem({ speed: m.speed ?? 0, gear: m.gear ?? 0, pos: m.pos ?? [0, 0, 0], steer: m.steer ?? 0, throttle: m.throttle ?? 0, brake: m.brake ?? 0 }) } break }
      case 'camera_switched': setView(m.view); break
      case 'vehicles': case 'vehicle_list': if (Array.isArray(m.vehicles)) { const list = m.vehicles.map((v: any) => v.id || v.name || v); setVehicles(list); setVehicle((cur) => (list.includes(cur) ? cur : list[0] || cur)) } break
      case 'error': setErr(m.message || m.error || 'server error'); setSpawning(false); break
      default: break
    }
  }, [view])

  const connect = () => {
    if (wsRef.current) return
    userClose.current = false
    setStatus('connecting'); setErr(null); setStalled(false)
    let ws: WebSocket
    try { ws = new WebSocket(url); ws.binaryType = 'blob' } catch (e: any) { setErr('WebSocket: ' + e.message); setStatus('closed'); return }
    wsRef.current = ws
    ws.onopen = () => { recordWire('EVT', 'evt', 'open', `connection OPEN → ${url}`); setStatus('open'); sendJSON({ type: 'list_vehicles' }) }
    ws.onmessage = handleMsg
    ws.onerror = () => { recordWire('EVT', 'evt', 'error', 'socket error'); setErr('socket error (see console)') }
    ws.onclose = () => {
      recordWire('EVT', 'evt', 'close', 'connection CLOSED')
      setStatus('closed'); setSessionActive(false); setVehicleId(null); setSessionInfo(null); setTelem(null); setSpawning(false); setStalled(false); streamingRef.current = false; setStreaming(false); wsRef.current = null
      if (!userClose.current) setErr('Connection dropped (network or tunnel). Click Connect to resume — your session ended, so spawn again.')
    }
  }
  const disconnect = () => { userClose.current = true; wsRef.current?.close(1000, 'user') }

  const spawn = () => { if (!open || sessionActive) return; setSpawning(true); setErr(null); setStalled(false); streamingRef.current = false; setStreaming(false); const w = isoWindow(); sendJSON({ type: 'start_session', start: w.start, end: w.end, vehicle }) }
  const endSession = () => { sendJSON({ type: 'end_session' }); setSessionActive(false); setVehicleId(null); setSessionInfo(null); setTelem(null); setMode('idle'); setStalled(false); streamingRef.current = false; setStreaming(false); setGear('D'); gearRef.current = 'D'; active.current = { s: 0, t: 0, b: 0, rev: false } }
  const respawn = () => sendJSON({ type: 'respawn' })
  const switchView = (v: string) => { setView(v); sendJSON({ type: 'camera_switch', view: v }) }

  // manual on-screen pad
  // Pads send immediately on press/release (assisted mode) so even a tap
  // shorter than one 50 ms loop tick still produces its packet.
  const padDown = (c: Partial<Ctrl>) => {
    active.current = { s: c.s ?? 0, t: c.t ?? 0, b: c.b ?? 0, rev: inReverse() }
    setMode('manual')
    if (opModeRef.current === 'assisted' && sessionActive) sendCtrl(active.current)
  }
  const padUp = () => {
    active.current = idleCtrl()
    if (mode === 'manual') setMode('idle')
    if (opModeRef.current === 'assisted' && sessionActive) sendCtrl(active.current)
  }

  // recording
  const startRec = () => { if (!sessionActive) return; rec.current = { on: true, segs: [], last: { ...active.current }, t: Date.now() }; setRecording(true); if (!manual) setManual(true) }
  const stopRec = () => {
    const r = rec.current; if (!r.on) return
    r.segs.push({ id: SID++, ...r.last, ms: Date.now() - r.t, label: ctrlLabel(r.last) })
    r.on = false; setRecording(false)
    setManual(false); active.current = idleCtrl() // release controls & keyboard after recording
    const captured = r.segs.filter((s) => s.ms >= 100) // drop sub-100ms noise
    if (captured.length) setSteps((p) => [...p, ...captured])
  }

  // step builder
  const addStep = () => {
    const p = PRESETS[presetIdx]; const ms = Math.max(100, Math.round(parseFloat(dur) * 1000) || 0)
    setSteps((s) => [...s, { id: SID++, s: p.s, t: p.t, b: p.b, rev: p.rev ?? false, ms, label: p.label }])
  }
  const delStep = (id: number) => setSteps((s) => s.filter((x) => x.id !== id))
  const moveStep = (id: number, d: number) => setSteps((s) => { const i = s.findIndex((x) => x.id === id); const j = i + d; if (i < 0 || j < 0 || j >= s.length) return s; const c = [...s];[c[i], c[j]] = [c[j], c[i]]; return c })
  const clearSteps = () => setSteps([])

  // wire-truth view: the exact control packet each step resolves to
  const stepPacket = (st: Step) => JSON.stringify({ type: 'control', s: st.s, t: st.t, b: st.b, rev: !!st.rev })
  const stepSends = (st: Step) => Math.max(1, Math.round(st.ms / 50))
  const copyPackets = async () => {
    const text = steps
      .map((st, i) => `// ${i + 1}. ${st.label} — hold ${(st.ms / 1000).toFixed(1)}s (~${stepSends(st)} sends @ 20 Hz)\n${stepPacket(st)}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopiedPkts(true); window.setTimeout(() => setCopiedPkts(false), 1500)
    } catch { /* clipboard unavailable */ }
  }

  // player
  const sleep = (ms: number) => new Promise<void>((res) => { const t = window.setInterval(() => { if (abort.current || Date.now() >= end) { window.clearInterval(t); res() } }, 25); const end = Date.now() + ms })
  const runScript = async () => {
    if (opModeRef.current === 'raw') return // RAW: the runner never transmits
    if (!sessionActive || !steps.length || mode === 'playing') return
    abort.current = false; setMode('playing'); setManual(false)
    for (const step of steps) {
      if (abort.current) break
      active.current = { s: step.s, t: step.t, b: step.b, rev: !!step.rev }; setPlayingId(step.id)
      await sleep(step.ms)
    }
    active.current = { s: 0, t: 0, b: 1, rev: false }; setPlayingId(null); setMode('idle')
    window.setTimeout(() => { if (mode !== 'manual') active.current = idleCtrl() }, 600)
  }
  const stopScript = () => { abort.current = true; setPlayingId(null); setMode('idle'); active.current = idleCtrl() }

  // save / load
  const persist = (list: Saved[]) => { setSaved(list); try { localStorage.setItem(LS_KEY, JSON.stringify(list)) } catch { /* ignore */ } }
  const saveScript = () => { const name = scriptName.trim(); if (!name || !steps.length) return; const list = [...saved.filter((s) => s.name !== name), { name, steps }]; persist(list); setScriptName('') }
  const loadScript = (s: Saved) => setSteps(s.steps.map((x) => ({ ...x, id: SID++ })))
  const delScript = (name: string) => persist(saved.filter((s) => s.name !== name))

  const totalMs = useMemo(() => steps.reduce((a, s) => a + s.ms, 0), [steps])
  const speedKmh = telem ? Math.round(telem.speed * 3.6) : 0

  // day/night dash mode — persisted, seeded from the OS preference
  const [theme, setTheme] = useState<'night' | 'day'>('night')
  useEffect(() => {
    try {
      const stored = localStorage.getItem('carla-dev-theme')
      if (stored === 'day' || stored === 'night') setTheme(stored)
      else if (window.matchMedia?.('(prefers-color-scheme: light)').matches) setTheme('day')
    } catch { /* ignore */ }
  }, [])
  const toggleTheme = () => setTheme((t) => {
    const next = t === 'night' ? 'day' : 'night'
    try { localStorage.setItem('carla-dev-theme', next) } catch { /* ignore */ }
    return next
  })

  return (
    <div className={`app ${theme === 'day' ? 'day' : ''}`}>
      <style>{CSS}</style>
      <div className="wrap">
        {/* nameplate masthead */}
        <header className="plate">
          <div className="plate-id">
            <span className="plate-mark"><Car className="icon" /></span>
            <div>
              <div className="plate-model">DRIVE API <span>· DEV CONSOLE</span></div>
              <div className="plate-sub">CARLA digital twin · drive-by-wire over WebSocket</div>
            </div>
          </div>
          <div className="lamps" aria-hidden="true">
            <span className={`lamp ok ${open ? 'on' : ''}`}>LINK</span>
            <span className={`lamp ${sessionActive ? 'on' : ''}`}>SESSION</span>
            <span className={`lamp rec ${recording ? 'on' : ''}`}>REC</span>
          </div>
          <div className="spring" />
          <div className="views" role="group" aria-label="Operating mode">
            <button className={opMode === 'assisted' ? 'on' : ''} onClick={() => switchOpMode('assisted')}
              title="ASSISTED — the console transmits for you: 20 Hz control loop, pads drive the car, the script runner sends.">
              Assisted
            </button>
            <button className={opMode === 'raw' ? 'on' : ''} onClick={() => switchOpMode('raw')}
              title="RAW — the console never sends a control packet on its own. The injector is the only TX path; pads and capture only author packets.">
              Raw
            </button>
          </div>
          <span className={`pill st-${status}`}><span className="dot" />{status}</span>
          <a className="modesw" href="./diagram.html" target="_blank" rel="noopener"
            title="How a connection becomes a car — interactive routing diagram (opens in a new tab)">
            <Network className="icon-sm" /> Routing
          </a>
          <button className="modesw" onClick={toggleTheme} title="Toggle day / night mode">
            {theme === 'night' ? <><Sun className="icon-sm" /> Day</> : <><Moon className="icon-sm" /> Night</>}
          </button>
        </header>

        {/* bench: endpoint + link controls */}
        <div className="bench">
          <div className="endpoint">
            <input className="field" value={url} spellCheck={false} onChange={(e) => setUrl(e.target.value)} aria-label="drive endpoint" />
            <button className="btn btn-accent" onClick={connect} disabled={open || status === 'connecting'}><Power className="icon" /> Connect</button>
            <button className="btn" onClick={disconnect} disabled={!open}><PowerOff className="icon" /> Disconnect</button>
          </div>
        </div>
        {err && <div className="err">⚠ {err}</div>}

        {/* session bar */}
        <div className="sess-bar">
          <select className="select" value={vehicle} disabled={sessionActive || !open} onChange={(e) => setVehicle(e.target.value)}>
            {vehicles.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          {!sessionActive
            ? <button className="btn btn-ok" onClick={spawn} disabled={!open || spawning}><Car className="icon" /> {spawning ? 'Spawning…' : 'Spawn vehicle'}</button>
            : <><button className="btn" onClick={respawn}><CircleDot className="icon" /> Respawn</button><button className="btn btn-crit" onClick={endSession}><Square className="icon" /> End session</button></>}
          <div className="sep" />
          <div className="kv"><span className="k">Vehicle ID</span><span className="v">{vehicleId ?? '—'}</span></div>
          <div className="kv" title={sessionInfo ? JSON.stringify(sessionInfo) : 'session_ready payload appears here'}>
            <span className="k">session_ready</span>
            <span className="v" style={{ fontSize: 12, color: sessionActive ? 'var(--ok)' : 'var(--faint)' }}>
              {sessionInfo
                ? `veh ${sessionInfo.vehicle_id} · ${(sessionInfo.sensor_actor_ids?.length ?? 0)} sensors · ${(sessionInfo.owned_actor_ids?.length ?? 0)} owned`
                : 'no session'}
            </span>
          </div>
          <div className="spring" />
          <div className="views">
            {VIEWS.map((v) => <button key={v} className={v === view ? 'on' : ''} disabled={!sessionActive} onClick={() => switchView(v)}>{v}</button>)}
          </div>
        </div>

        {/* main: live view | recorder */}
        <div className="grid2">
          {/* live view */}
          <div className="panel">
            <div className="panel-hd">
              <span className="eyebrow">Live view · camera + telemetry</span>
              <span style={{ display: 'inline-flex', gap: 10 }}>
                {opMode === 'raw' && <span className="eyebrow" style={{ color: 'var(--accent)' }} title="No automatic control packets — telemetry only arrives in reply to packets you inject.">raw · manual TX only</span>}
                <span className="eyebrow" style={{ color: stalled ? 'var(--crit)' : sessionActive && streaming ? 'var(--ok)' : 'var(--faint)' }}>{stalled ? 'stalled' : sessionActive ? (streaming ? 'streaming' : 'starting…') : 'idle'}</span>
              </span>
            </div>
            <div className="panel-bd">
              <div className="stage">
                <img ref={imgRef} alt="" style={{ display: sessionActive && streaming && !stalled ? 'block' : 'none' }} />
                {!sessionActive && <div className="placeholder">Spawn a vehicle to start the camera stream</div>}
                {sessionActive && !streaming && !stalled && <div className="placeholder">Starting camera stream…<br />(scene reconstruction, ~5s)</div>}
                {stalled && <div className="placeholder" style={{ color: '#E58A8A' }}>⚠ Camera stream stalled — the connection may have dropped.<br />Reconnect to resume.</div>}
              </div>
              <div className="hud">
                <div className="speedo"><div className="n tnum">{speedKmh}</div><div className="u">km/h</div></div>
                <Meter label="Throttle" val={telem?.throttle ?? 0} color="var(--ok)" />
                <Meter label="Brake" val={telem?.brake ?? 0} color="var(--crit)" />
                <SteerMeter val={telem?.steer ?? 0} />
              </div>
              {telem && <div className="hint">gear {telem.gear} · pos [{telem.pos.map((n) => n.toFixed(1)).join(', ')}]</div>}
            </div>
          </div>

          {/* packet injector — same row & height as the live view */}
          <div className="panel pcol">
            <div className="panel-hd">
              <span className="eyebrow">Packet injector · raw TX</span>
              <span className="eyebrow" style={{ color: open ? 'var(--ok)' : 'var(--faint)' }}>{open ? 'link open' : 'link closed'}</span>
            </div>
            <div className="panel-bd pfill">
              <textarea
                className="inj injfill" value={inj} spellCheck={false} aria-label="raw packet"
                placeholder='Anything here goes down the WebSocket exactly as typed — valid or not.'
                onChange={(e) => setInj(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); injectRaw() } }}
              />
              <div className="injrow">
                <button className="btn btn-accent" disabled={!open || !inj.length} onClick={injectRaw}><ChevronRight className="icon" /> Send raw</button>
                <span className="injnote">Sent verbatim — no validation. Enter sends · Shift+Enter = newline. Watch the flight recorder for the server's reaction (or its silence).</span>
              </div>
            </div>
          </div>
        </div>

        {/* script deck — full-width beneath the bench row */}
        <div className="panel" style={{ marginTop: 14 }}>
            <div className="panel-hd">
              <span className="eyebrow">Input script · convenience builder (valid control sequences)</span>
              {recording ? <span className="recdot"><span className="d" />RECORDING</span> : <span className="eyebrow">{steps.length} steps · {(totalMs / 1000).toFixed(1)}s</span>}
            </div>
            <div className="panel-bd">
              <div className="deck">
              <div>
              {/* live capture */}
              <span className="eyebrow">
                {opMode === 'assisted'
                  ? 'Live capture — drive with W A S D (or the pad), record the timing'
                  : 'Live capture (raw) — inputs are recorded as steps but NEVER transmitted'}
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 9 }}>
                <span className="eyebrow">Gear</span>
                <div className="views vsm" role="group" aria-label="Gear">
                  <button className={gear === 'D' ? 'on' : ''} onClick={() => pickGear('D')} title='Drive — throttle packets carry "rev":false'>D</button>
                  <button className={gear === 'R' ? 'on' : ''} onClick={() => pickGear('R')} title='Reverse — throttle packets carry "rev":true'>R</button>
                </div>
                <span className="hint" style={{ margin: 0 }}>{gear === 'R' ? 'gas now backs up — "rev":true' : 'forward'}</span>
              </div>
              <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', marginTop: 8, flexWrap: 'wrap' }}>
                <div className="drivepad">
                  <PadBtn cls="gas" active={mode === 'manual' && active.current.t > 0} disabled={!sessionActive} onDown={() => padDown({ t: 1 })} onUp={padUp}><ArrowUp className="icon-sm" />W</PadBtn>
                  <PadBtn cls="a" active={mode === 'manual' && active.current.s < 0} disabled={!sessionActive} onDown={() => padDown({ s: -0.7 })} onUp={padUp}><ArrowLeft className="icon-sm" />A</PadBtn>
                  <PadBtn cls="brk" active={mode === 'manual' && active.current.b > 0} disabled={!sessionActive} onDown={() => padDown({ b: 1 })} onUp={padUp}><ArrowDown className="icon-sm" />S</PadBtn>
                  <PadBtn cls="d" active={mode === 'manual' && active.current.s > 0} disabled={!sessionActive} onDown={() => padDown({ s: 0.7 })} onUp={padUp}><ArrowRight className="icon-sm" />D</PadBtn>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                  <button className={`btn btn-sm ${manual ? 'btn-accent' : ''}`} disabled={!sessionActive} onClick={() => setManual((m) => !m)}>{manual ? 'Keyboard ON' : 'Enable keyboard'}</button>
                  {!recording
                    ? <button className="btn btn-sm btn-rec" disabled={!sessionActive} onClick={startRec}><Circle className="icon-sm" /> Record</button>
                    : <button className="btn btn-sm btn-crit" onClick={stopRec}><Square className="icon-sm" /> Stop &amp; add</button>}
                </div>
              </div>

              </div>

              <div>
              {/* step builder */}
              <div className="addrow" style={{ marginTop: 0 }}>
                <span className="eyebrow">Add step</span>
                <select className="select" value={presetIdx} onChange={(e) => setPresetIdx(+e.target.value)}>
                  {PRESETS.map((p, i) => <option key={p.label} value={i}>{p.label}</option>)}
                </select>
                <input className="field dur mono" value={dur} onChange={(e) => setDur(e.target.value)} inputMode="decimal" aria-label="seconds" />
                <span className="hint" style={{ margin: 0 }}>sec</span>
                <button className="btn btn-sm" onClick={addStep}><Plus className="icon-sm" /> Add</button>
              </div>

              {/* sequence: friendly steps ⇄ the exact packets they become */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginTop: 11, flexWrap: 'wrap' }}>
                <span className="eyebrow">Sequence</span>
                <div className="views vsm">
                  <button className={scriptView === 'steps' ? 'on' : ''} onClick={() => setScriptView('steps')}>steps</button>
                  <button className={scriptView === 'packets' ? 'on' : ''} onClick={() => setScriptView('packets')}>packets</button>
                </div>
                {scriptView === 'packets' && steps.length > 0 && (
                  <button className="btn btn-sm" onClick={copyPackets}>{copiedPkts ? 'Copied' : 'Copy'}</button>
                )}
              </div>
              {scriptView === 'steps' ? (
                <div className="steps">
                  {steps.length === 0 ? <div className="empty">No steps yet — record above or add a step.</div> : steps.map((st, i) => (
                    <div key={st.id} className={`step ${playingId === st.id ? 'on' : ''}`}>
                      <span className="ix">{i + 1}</span>
                      <span className="lb">{st.label}</span>
                      <span className="du">{(st.ms / 1000).toFixed(1)}s</span>
                      <span className="ops">
                        <button className="iconbtn" disabled={i === 0} onClick={() => moveStep(st.id, -1)}><ChevronUp className="icon-sm" /></button>
                        <button className="iconbtn" disabled={i === steps.length - 1} onClick={() => moveStep(st.id, 1)}><ChevronDown className="icon-sm" /></button>
                        <button className="iconbtn" onClick={() => delStep(st.id)}><Trash2 className="icon-sm" /></button>
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="pktscript">
                  {steps.length === 0 ? (
                    <div className="wire-empty" style={{ padding: 4 }}>No steps yet — this view shows the exact control packets your script sends.</div>
                  ) : (
                    <>
                      {steps.map((st, i) => (
                        <div key={st.id} className={`pktline ${playingId === st.id ? 'on' : ''}`}>
                          <span className="c">{'//'} {i + 1}. {st.label} — hold {(st.ms / 1000).toFixed(1)}s (~{stepSends(st)} sends @ 20 Hz)</span>
                          <span style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                            <code style={{ flex: 1 }}>{stepPacket(st)}</code>
                            <button className="iconbtn" title="Load this packet into the injector" onClick={() => setInj(stepPacket(st))}>
                              <ChevronRight className="icon-sm" />
                            </button>
                          </span>
                        </div>
                      ))}
                      <div className="pktfoot">
                        {opMode === 'assisted'
                          ? "Each step's packet repeats at 20 Hz for its duration — exactly what the runner puts on the wire (watch the flight recorder while it plays)."
                          : 'RAW — click ▸ on a packet to load it into the injector, then send it yourself. The console will not transmit these for you.'}
                      </div>
                    </>
                  )}
                </div>
              )}

              {/* run */}
              <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                {opMode === 'raw' ? (
                  <span className="hint" style={{ margin: 0, color: 'var(--accent)' }}>
                    RAW — the runner is disabled. Open the packets view and load each one into the injector yourself.
                  </span>
                ) : mode !== 'playing'
                  ? <button className="btn btn-accent" disabled={!sessionActive || !steps.length} onClick={runScript}><Play className="icon" /> Run script</button>
                  : <button className="btn btn-crit" onClick={stopScript}><Square className="icon" /> Stop</button>}
                <button className="btn btn-sm" disabled={!steps.length} onClick={clearSteps}><Trash2 className="icon-sm" /> Clear</button>
              </div>

              {/* save / load */}
              <div className="savedrow">
                <input className="field mono" style={{ width: 150 }} value={scriptName} placeholder="script name" onChange={(e) => setScriptName(e.target.value)} />
                <button className="btn btn-sm" disabled={!scriptName.trim() || !steps.length} onClick={saveScript}><Save className="icon-sm" /> Save</button>
                {saved.map((s) => (
                  <span key={s.name} className="chip"><FolderOpen className="icon-sm" style={{ cursor: 'pointer' }} onClick={() => loadScript(s)} /><span style={{ cursor: 'pointer' }} onClick={() => loadScript(s)}>{s.name}</span><button onClick={() => delScript(s.name)} title="delete"><Trash2 className="icon-sm" /></button></span>
                ))}
              </div>
              </div>
              </div>
            </div>
          </div>

        {/* wire log — live packet inspector */}
        <div className="panel" style={{ marginTop: 16 }}>
          <div className="panel-hd">
            <span className="eyebrow" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <Activity className="icon-sm" /> Flight recorder · every socket frame, verbatim
            </span>
            <div className="wire-controls">
              {(['control', 'telemetry', 'binary', 'other'] as const).map((k) => (
                <label key={k} className="wcheck">
                  <input type="checkbox" checked={wireFilters[k]} onChange={() => setWireFilters((f) => ({ ...f, [k]: !f[k] }))} />
                  {k}
                </label>
              ))}
              <button className="btn btn-sm" onClick={() => setWirePaused((p) => !p)}>
                {wirePaused ? <><Play className="icon-sm" /> Resume</> : <><Pause className="icon-sm" /> Pause</>}
              </button>
              <button className="btn btn-sm" onClick={wireDownload} disabled={wireTotal.current === 0} title="Download the full buffer as JSON Lines">
                <Download className="icon-sm" /> .jsonl
              </button>
              <button className="btn btn-sm" onClick={wireClear} disabled={wireTotal.current === 0}><Trash2 className="icon-sm" /></button>
              <span className="wire-count">
                {wireShown.length} shown · {wireTotal.current} total{wireTotal.current > WIRE_CAP ? ` · last ${WIRE_CAP} kept` : ''}
              </span>
            </div>
          </div>
          <div className="panel-bd" style={{ padding: 8 }}>
            <WireStream rows={wireShown} />
          </div>
        </div>

        {/* documentation (memoized) */}
        <DocList />
      </div>
    </div>
  )
}

function WireStream({ rows }: { rows: WirePkt[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  useEffect(() => {
    const el = ref.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  })
  return (
    <div
      ref={ref}
      className="wire-stream"
      onScroll={(e) => {
        const el = e.currentTarget
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
    >
      {rows.length === 0 ? (
        <div className="wire-empty">No frames yet — connect and spawn a vehicle to see every packet live.</div>
      ) : (
        rows.map((p) => (
          <div key={p.i} className={`wrow ${p.kind === 'bin' ? 'bin' : p.kind === 'evt' ? 'evt' : p.dir === 'TX' ? 'tx' : 'rx'}`}>
            <span className="wt">{p.t}</span>
            <span className="wd">{p.dir === 'EVT' ? '•' : p.dir}</span>
            <span className="wp">{p.kind === 'bin' ? `${p.data} (${p.bytes} B)` : p.data}</span>
          </div>
        ))
      )}
    </div>
  )
}

function Meter({ label, val, color }: { label: string; val: number; color: string }) {
  return (
    <div className="meter">
      <span className="lab">{label}</span>
      <div className="track"><div className="fill" style={{ width: `${Math.round(Math.min(1, Math.max(0, val)) * 100)}%`, background: color }} /></div>
      <span className="val">{Math.round(val * 100)}%</span>
    </div>
  )
}
function SteerMeter({ val }: { val: number }) {
  const pct = Math.min(1, Math.max(-1, val))
  const left = pct < 0 ? `${50 + pct * 50}%` : '50%'
  const w = Math.abs(pct) * 50
  return (
    <div className="meter">
      <span className="lab">Steer</span>
      <div className="track steerwrap"><span className="mid" /><div className="fill" style={{ position: 'absolute', left, width: `${w}%`, background: 'var(--accent)' }} /></div>
      <span className="val">{val.toFixed(2)}</span>
    </div>
  )
}
function PadBtn({ children, cls, active, disabled, onDown, onUp }: any) {
  return (
    <button className={`pad ${cls || ''} ${active ? 'act' : ''}`} disabled={disabled}
      onPointerDown={(e) => { e.preventDefault(); try { (e.target as HTMLElement).setPointerCapture?.(e.pointerId) } catch { /* stale/synthetic pointer id — capture is a nicety, never block the press */ } onDown() }}
      onPointerUp={onUp} onPointerLeave={(e) => { if ((e.buttons & 1) === 0) return; onUp() }} onPointerCancel={onUp}>
      {children}
    </button>
  )
}
