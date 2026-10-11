/* FWMS floor view — canvas renderer ported from the WMS-App reference
 * simulator. The warehouse geometry is the reference layout; everything on it
 * (racked stock, staging pallets, workers, docks, carts, events) comes from
 * fwms.api.floorview.dashboard, polled every 2s. Workers walk real routes
 * between their current task bins using the same A* pathfinding. */
(() => {
'use strict';

/* ---------------------------------------------------------------- map (reference geometry) */
const W = 66, H = 38;
const OUT = 0, FLOOR = 1, WALL = 2, RACK = 3, BLOCK = 4, DOOR = 5;
const idx = (x, y) => y * W + x;
const grid = new Uint8Array(W * H);
const setRect = (x0, y0, x1, y1, v) => { for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) grid[idx(x, y)] = v; };

setRect(3, 1, 62, 36, WALL);
setRect(4, 2, 61, 35, FLOOR);
const IN_DOOR_Y = [6, 11, 16, 21];
const OUT_DOOR_Y = [7, 14, 22, 29];
IN_DOOR_Y.forEach(y => grid[idx(3, y)] = DOOR);
OUT_DOOR_Y.forEach(y => grid[idx(62, y)] = DOOR);

// receiving staging spots (pallet footprints)
const SPOT_DEF = [];
for (const x of [6, 9]) for (let y = 3; y <= 21; y += 2) {
	SPOT_DEF.push({ x, y, ax: x === 6 ? 7 : 8, ay: y, code: 'STG-' + String(SPOT_DEF.length + 1).padStart(2, '0') });
	grid[idx(x, y)] = BLOCK;
}
setRect(6, 25, 7, 25, BLOCK);   // QC bench
setRect(9, 24, 10, 25, BLOCK);  // quarantine
setRect(6, 31, 8, 31, BLOCK);   // returns desk
const QTN = { ax: 8, ay: 25, x: 9, y: 25 };

// racks
const AISLES = 'ABCDEFGHI';
for (let p = 0; p < 8; p++) {
	const px = 14 + 4 * p;
	for (const x of [px, px + 1]) for (let y = 4; y <= 32; y++) {
		if (y >= 17 && y <= 19) continue;
		grid[idx(x, y)] = RACK;
	}
}

// pack stations, conveyor, outbound lanes, supervisor desk
const ST_Y = [4, 9, 14, 20, 25, 30];
ST_Y.forEach(y => { setRect(49, y, 50, y, BLOCK); grid[idx(52, y)] = BLOCK; });
setRect(53, 3, 53, 31, BLOCK);
const CARRIERS = ['PARCEL', 'EXPRESS', 'LTL', 'POSTAL'];
OUT_DOOR_Y.forEach(y => setRect(54, y, 59, y, BLOCK));
setRect(49, 33, 50, 33, BLOCK);

const walkable = i => grid[i] === FLOOR || grid[i] === DOOR;

const ZONES = {
	receiving: { x: 0, y: 2, w: 11, h: 21, label: 'RECEIVING · STAGING', tint: '#0891B2' },
	qc:        { x: 4, y: 23, w: 7, h: 5, label: 'QC INSPECTION', tint: '#16A34A' },
	returns:   { x: 4, y: 28, w: 7, h: 8, label: 'RETURNS', tint: '#92400E' },
	storage:   { x: 12, y: 2, w: 34, h: 34, label: 'RESERVE STORAGE · AISLES A–I', tint: '#44617B' },
	pack:      { x: 47, y: 2, w: 6, h: 30, label: 'PACK', tint: '#DB2777' },
	outbound:  { x: 53, y: 2, w: 13, h: 30, label: 'SORT · LOAD', tint: '#EA580C' },
	office:    { x: 47, y: 32, w: 6, h: 4, label: 'SUPERVISOR', tint: '#E11D48' },
	equip:     { x: 53, y: 32, w: 9, h: 4, label: 'EQUIPMENT', tint: '#CA8A04' },
	orders:    { x: 12, y: 2, w: 41, h: 34, label: '', tint: '#2563EB' },
	all:       { x: 4, y: 2, w: 58, h: 34, label: '', tint: '#E11D48' },
};

/* ---------------------------------------------------------------- roles */
const ROLES = {
	supervisor: { label: 'Supervisor',        short: 'SUP', color: '#E11D48', speed: 2.3 },
	receiver:   { label: 'Receiver',          short: 'RCV', color: '#0891B2', speed: 2.4 },
	qc:         { label: 'QC inspector',      short: 'QCI', color: '#16A34A', speed: 2.3 },
	forklift:   { label: 'Forklift putaway',  short: 'FLT', color: '#EAB308', speed: 3.3, vehicle: true },
	picker:     { label: 'Picker',            short: 'PCK', color: '#2563EB', speed: 2.5 },
	packer:     { label: 'Packer',            short: 'PAK', color: '#DB2777', speed: 2.3 },
	loader:     { label: 'Loader',            short: 'LDR', color: '#EA580C', speed: 2.4 },
	inventory:  { label: 'Inventory control', short: 'INV', color: '#7C3AED', speed: 2.3 },
	returns:    { label: 'Returns clerk',     short: 'RTN', color: '#92400E', speed: 2.3 },
};
const ROLE_LABEL = Object.fromEntries(Object.entries(ROLES).map(([k, v]) => [k, v.label]));

const HOMES = {
	receiver: [[5, 4], [5, 9], [5, 14], [5, 19], [5, 6], [5, 12]],
	qc: [[8, 26], [7, 26], [8, 27]],
	forklift: [[55, 34], [57, 34], [59, 34], [43, 34], [45, 34], [47, 35], [42, 34], [44, 35]],
	picker: [[44, 34], [45, 35], [46, 34], [43, 35], [46, 35], [45, 34], [44, 35], [43, 34]],
	packer: [[48, 4], [48, 9], [48, 14], [48, 20], [48, 25], [48, 30], [48, 6], [48, 22]],
	loader: [[60, 8], [60, 15], [60, 23], [60, 29]],
	inventory: [[46, 2], [47, 2]],
	returns: [[7, 32]],
	supervisor: [[50, 34]],
};

const TASK_BUBBLE = {
	Receive: a => `Receiving ${a.item || ''}`,
	Putaway: a => `Put away → ${a.to_bin || 'bin'}`,
	Pick: a => `Pick ${a.item || ''} × ${a.qty || ''}`,
	Replenish: a => `Replenish → ${a.to_bin || ''}`,
	Count: a => `Count ${a.to_bin || ''}`,
	Pack: a => `Pack ${a.lpn || ''}`,
	Load: a => `Load at dock`,
};
const TASK_WHY = {
	Receive: 'A trailer was checked in against its ASN; this worker is scanning pallets to staging.',
	Putaway: 'The WMS directed this LPN to the suggested bin — pick face if slotted and below max, else bulk.',
	Pick: 'Released order line; the offered batch is FEFO (earliest expiry first) from pick-face stock.',
	Replenish: 'The pick face fell below its min; bulk stock tops it back up to max.',
	Count: 'A cycle-count program scheduled this bin by ABC class; counts are blind by default.',
	Pack: 'Cart contents are verified against the Delivery Note, then sealed with a ZPL label.',
	Load: 'Cartons are staged at the dock; closing the trailer submits the Delivery Note.',
};

/* ---------------------------------------------------------------- state */
const S = {
	data: null,            // last dashboard payload
	slots: [], staging: [], qcCount: 0, laneCartons: 0, inboundLpns: 0,
	docks: [], packQueue: 0,
	agents: new Map(),     // worker name -> agent
	aisleHot: new Float64Array(9),
	seq: 0,
};
const UI = { playing: true, speed: 1, bubbles: 'scans', filter: 'all', selected: null, tourZone: 'receiving', tour: 0, booted: false };
const STAGES = {
	'Receiving & QC':    { code: 'INBOUND', stage: 'inbound', color: '#0891B2' },
	'Putaway & Storage': { code: 'STORAGE', stage: 'storage', color: '#CA8A04' },
	'Orders & Picking':  { code: 'ORDER',   stage: 'pick',    color: '#2563EB' },
	'Packing':           { code: 'PACK',    stage: 'pack',    color: '#DB2777' },
	'Sort Load & Ship':  { code: 'SHIP',    stage: 'ship',    color: '#EA580C' },
	'Inventory Control': { code: 'INV',     stage: 'inv',     color: '#7C3AED' },
	'Returns':           { code: 'RTN',     stage: 'returns', color: '#A16207' },
	'Alerts & Supervisor': { code: 'ALERT', stage: 'alert',   color: '#DC2626' },
	'Carrier':  { code: 'CARRIER',  stage: 'ship',  color: '#EA580C' },
	'Customs':  { code: 'CUSTOMS',  stage: 'alert', color: '#DC2626' },
	'Automation': { code: 'AUTO',   stage: 'alert', color: '#DC2626' },
};
const FILTERS = {
	all: () => true,
	scans: e => e.is_scan,
	'Receiving & QC': e => e.category === 'Receiving & QC',
	'Putaway & Storage': e => e.category === 'Putaway & Storage',
	'Orders & Picking': e => e.category === 'Orders & Picking',
	'Packing': e => e.category === 'Packing',
	'Sort Load & Ship': e => e.category === 'Sort Load & Ship',
	'Inventory Control': e => e.category === 'Inventory Control',
	'Returns': e => e.category === 'Returns',
	'Alerts & Supervisor': e => e.category === 'Alerts & Supervisor',
};

/* ---------------------------------------------------------------- api */
// v16 requires X-Frappe-CSRF-Token on POSTs from browser sessions (the RF
// app solved this in app.js; same pattern here): fetch once, retry once on
// a rotated session.
let CSRF = null;
let csrfFetch = null;

async function ensureCsrf(force = false) {
	if (CSRF && !force) return CSRF;
	if (!csrfFetch) {
		csrfFetch = fetch('/api/method/fwms.api.pwa.csrf', { method: 'POST' })
			.then((r) => r.json())
			.then((d) => { CSRF = d.message || null; })
			.catch(() => { CSRF = null; })
			.finally(() => { csrfFetch = null; });
	}
	await csrfFetch;
	return CSRF;
}

async function api(method, params = {}, _retry = true) {
	await ensureCsrf();
	const body = new URLSearchParams(params);
	const headers = {
		'Content-Type': 'application/x-www-form-urlencoded',
		'Accept': 'application/json',
	};
	if (CSRF) headers['X-Frappe-CSRF-Token'] = CSRF;
	const r = await fetch(`/api/method/${method}`, { method: 'POST', headers, body });
	const d = await r.json().catch(() => ({}));
	// session rotated (login/logout) -> stale token: refetch once and retry
	if ((d.exc_type === 'CSRFTokenError' || /Invalid Request/.test(d.exc || '')) && _retry) {
		CSRF = null;
		await ensureCsrf(true);
		return api(method, params, false);
	}
	if (!r.ok || d.exc) throw new Error(String(d.exc || r.status));
	return d.message;
}

/* ---------------------------------------------------------------- data sync */
let seenEvents = new Set();
let lastPoll = 0, pollTimer = null;

async function poll() {
	try {
		const D = await api('fwms.api.floorview.dashboard');
		S.data = D;
		S.slots = D.slots || [];
		S.staging = (D.staging && D.staging.receiving) || [];
		S.qcCount = (D.staging && D.staging.qc_count) || 0;
		S.laneCartons = D.lane_cartons || 0;
		S.inboundLpns = D.inbound_lpns || 0;
		S.docks = D.docks || [];
		S.packQueue = D.pack_queue || 0;
		syncWorkers(D);
		renderKpis(D);
		if (!UI.booted) { buildLog(D); UI.booted = true; applyTour(0); }
		else mergeLog(D.events || []);
		renderInspector();
	} catch (e) {
		// not logged in (or session expired) -> desk login, come back here
		if (/403|Permission/i.test(String(e))) {
			window.location = '/login?redirect-to=/floorview';
			return;
		}
		console.warn('poll failed', e);
	}
}

function syncWorkers(D) {
	const taskByWorker = D.task_by_worker || {};
	const homeCounters = {};
	for (const w of D.workers || []) {
		let a = S.agents.get(w.name);
		if (!a) {
			a = {
				worker: w.name, code: w.worker_code, first: (w.person_name || '?').split(' ')[0],
				role: w.role, x: 0, y: 0, tile: 0, mv: -1, path: [], goal: -1,
				face: { x: 1, y: 0 }, bubble: '', scanT: 0, waitT: 0,
				scans: w.scans_today || 0, tasks: w.tasks_completed || 0,
			};
			const home = homeFor(a.role, homeCounters);
			a.x = home[0]; a.y = home[1]; a.tile = idx(a.x, a.y); a.home = home;
			S.agents.set(w.name, a);
		}
		a.role = w.role; a.scans = w.scans_today || 0; a.tasks = w.tasks_completed || 0;
		const t = taskByWorker[w.name];
		a.task = t || null;
		a.bubble = t ? (TASK_BUBBLE[t.task_type] || (() => t.task_type))(t) : '';
		let target = null;
		if (t) {
			const xy = t.to_xy && t.to_xy[0] != null ? t.to_xy : (t.from_xy && t.from_xy[0] != null ? t.from_xy : null);
			if (xy) target = xy;
		}
		if (target) {
			const goal = idx(target[0], target[1]);
			if (goal !== a.goal && walkable(goal)) { a.goal = goal; a.path = findPath(a.tile, goal, a) || []; }
			a.scanT = onTile(a, target) ? a.scanT + 1 : 0;
		} else {
			const goal = idx(a.home[0], a.home[1]);
			if (a.tile !== goal && walkable(goal)) { a.goal = goal; a.path = findPath(a.tile, goal, a) || []; }
			else a.path = [];
		}
	}
	// drop agents for deactivated workers
	const live = new Set((D.workers || []).map(w => w.name));
	for (const [name, a] of S.agents) if (!live.has(name)) S.agents.delete(name);
}

function homeFor(role, counters) {
	const h = HOMES[role] || HOMES.forklift;
	const i = (counters[role] = (counters[role] || 0) + 1) - 1;
	return h[i % h.length];
}
const onTile = (a, xy) => Math.floor(a.x) === xy[0] && Math.floor(a.y) === xy[1];

/* ---------------------------------------------------------------- pathfinding (reference A*) */
const gScore = new Float32Array(W * H), came = new Int32Array(W * H), seen = new Int32Array(W * H), closed = new Int32Array(W * H);
let gen = 0;
function findPath(start, goal, a) {
	if (start === goal) return [];
	if (!walkable(goal)) return null;
	gen++;
	const gx = goal % W, gy = (goal / W) | 0;
	const heap = [], hf = [];
	const push = (i, f) => { heap.push(i); hf.push(f); let c = heap.length - 1; while (c > 0) { const p = (c - 1) >> 1; if (hf[p] <= hf[c]) break; [heap[p], heap[c]] = [heap[c], heap[p]]; [hf[p], hf[c]] = [hf[c], hf[p]]; c = p; } };
	const pop = () => { const top = heap[0]; const li = heap.pop(), lf = hf.pop(); if (heap.length) { heap[0] = li; hf[0] = lf; let c = 0; for (;;) { const l = 2 * c + 1, r = l + 1; let m = c; if (l < heap.length && hf[l] < hf[m]) m = l; if (r < heap.length && hf[r] < hf[m]) m = r; if (m === c) break; [heap[m], heap[c]] = [heap[c], heap[m]]; [hf[m], hf[c]] = [hf[c], hf[m]]; c = m; } } return top; };
	seen[start] = gen; gScore[start] = 0; came[start] = -1;
	push(start, 0);
	let found = false, it = 0;
	while (heap.length && it++ < 6000) {
		const cur = pop();
		if (closed[cur] === gen) continue;
		closed[cur] = gen;
		if (cur === goal) { found = true; break; }
		const cx = cur % W, cy = (cur / W) | 0;
		for (let d = 0; d < 4; d++) {
			const nx = cx + (d === 0 ? 1 : d === 1 ? -1 : 0), ny = cy + (d === 2 ? 1 : d === 3 ? -1 : 0);
			if (nx < 0 || ny < 0 || nx >= W || ny >= H) continue;
			const n = idx(nx, ny);
			if (!walkable(n) || closed[n] === gen) continue;
			const g = gScore[cur] + 1;
			if (seen[n] !== gen || g < gScore[n]) { seen[n] = gen; gScore[n] = g; came[n] = cur; push(n, g + Math.abs(nx - gx) + Math.abs(ny - gy)); }
		}
	}
	if (!found) return null;
	const path = [];
	for (let c = goal; c !== start; c = came[c]) path.push(c);
	return path.reverse();
}

function moveAgent(a, dt) {
	let budget = (ROLES[a.role]?.speed || 2.4) * dt * UI.speed;
	let guard = 0;
	while (budget > 1e-6 && guard++ < 8) {
		if (a.mv < 0) {
			if (!a.path.length) return;
			a.mv = a.path[0];
		}
		const tx = a.mv % W, ty = (a.mv / W) | 0;
		const dx = tx - a.x, dy = ty - a.y;
		if (dx || dy) a.face = dx ? { x: Math.sign(dx), y: 0 } : { x: 0, y: Math.sign(dy) };
		const d = Math.abs(dx) + Math.abs(dy);
		if (d <= budget) { a.x = tx; a.y = ty; budget -= d; a.tile = a.mv; a.mv = -1; a.path.shift(); }
		else { if (dx) a.x += Math.sign(dx) * budget; else a.y += Math.sign(dy) * budget; budget = 0; }
	}
}

/* ---------------------------------------------------------------- rendering */
const cv = document.getElementById('cv'), ctx = cv.getContext('2d'), wrap = document.getElementById('wrap');
let T = 16, DPR = 1;
const CSS = getComputedStyle(document.documentElement);
const C = {};
for (const k of ['bg', 'panel', 'panel2', 'ink', 'muted', 'line', 'floor', 'yard', 'wall', 'rack', 'stock', 'mark', 'good', 'bad', 'accent'])
	C[k] = CSS.getPropertyValue('--' + k).trim();
C.za = 0.09;

function resize() {
	const w = wrap.clientWidth;
	T = Math.max(10, w / W);
	DPR = window.devicePixelRatio || 1;
	cv.style.width = W * T + 'px'; cv.style.height = H * T + 'px';
	cv.width = Math.round(W * T * DPR); cv.height = Math.round(H * T * DPR);
}
const hexA = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`; };
function rr(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h); }
const PALLET = '#B8895A', WRAP = '#E7D8BD', KRAFT = '#C9A06A';

function label(text, x, y, size, color, align = 'left', weight = 600, font = 'cond') {
	ctx.font = `${weight} ${size}px ${font === 'cond' ? '"Barlow Condensed","Arial Narrow",sans-serif' : font === 'mono' ? '"JetBrains Mono",Consolas,monospace' : 'Barlow,"Segoe UI",sans-serif'}`;
	ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = 'middle'; ctx.fillText(text, x, y);
}
function drawPallet(px, py, s, hold) {
	ctx.fillStyle = PALLET; rr(px - s / 2, py - s / 2, s, s, 2); ctx.fill();
	ctx.fillStyle = WRAP; rr(px - s * 0.36, py - s * 0.36, s * 0.72, s * 0.72, 2); ctx.fill();
	ctx.strokeStyle = hexA('#7A5530', 0.55); ctx.lineWidth = 1;
	ctx.beginPath(); ctx.moveTo(px - s * 0.36, py); ctx.lineTo(px + s * 0.36, py); ctx.moveTo(px, py - s * 0.36); ctx.lineTo(px, py + s * 0.36); ctx.stroke();
	if (hold) { ctx.strokeStyle = C.bad; ctx.lineWidth = 2; ctx.setLineDash([3, 2]); rr(px - s / 2 - 2, py - s / 2 - 2, s + 4, s + 4, 3); ctx.stroke(); ctx.setLineDash([]); }
}

function draw() {
	ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
	ctx.fillStyle = C.yard; ctx.fillRect(0, 0, W * T, H * T);
	ctx.strokeStyle = hexA('#ffffff', 0.06); ctx.lineWidth = 1;
	for (let y = 2; y < 36; y += 1) { ctx.beginPath(); ctx.moveTo(0, y * T); ctx.lineTo(3 * T, y * T); ctx.moveTo(63 * T, y * T); ctx.lineTo(66 * T, y * T); ctx.stroke(); }
	ctx.fillStyle = C.floor; ctx.fillRect(4 * T, 2 * T, 58 * T, 34 * T);
	for (const key of ['receiving', 'qc', 'returns', 'pack', 'outbound', 'office', 'equip']) {
		const z = ZONES[key]; const x0 = Math.max(z.x, 4), x1 = Math.min(z.x + z.w, 62);
		ctx.fillStyle = hexA(z.tint, C.za); ctx.fillRect(x0 * T, z.y * T, (x1 - x0) * T, z.h * T);
	}
	for (const key of ['receiving', 'qc', 'returns', 'storage', 'pack', 'outbound', 'office', 'equip']) {
		const z = ZONES[key];
		label(z.label, (Math.max(z.x, 4) + 0.3) * T, (z.y + 0.45) * T, Math.max(9, T * 0.62), C.muted, 'left', 700);
	}
	// walkway markings
	ctx.strokeStyle = hexA(C.mark, 0.75); ctx.lineWidth = Math.max(1.5, T * 0.09);
	ctx.setLineDash([T * 0.6, T * 0.35]);
	for (const y of [17, 20]) { ctx.beginPath(); ctx.moveTo(12 * T, y * T); ctx.lineTo(46 * T, y * T); ctx.stroke(); }
	ctx.beginPath(); ctx.moveTo(11.5 * T, 3 * T); ctx.lineTo(11.5 * T, 35 * T); ctx.moveTo(46.5 * T, 3 * T); ctx.lineTo(46.5 * T, 35 * T); ctx.stroke();
	ctx.setLineDash([]);

	// racks + real slots
	for (let p = 0; p < 8; p++) {
		const px = 14 + 4 * p;
		for (const [y0, y1] of [[4, 16], [20, 32]]) {
			ctx.fillStyle = hexA(C.rack, 0.28);
			ctx.fillRect(px * T, y0 * T, 2 * T, (y1 - y0 + 1) * T);
			ctx.strokeStyle = C.rack; ctx.lineWidth = 1.5;
			ctx.strokeRect(px * T + 0.75, y0 * T + 0.75, 2 * T - 1.5, (y1 - y0 + 1) * T - 1.5);
			ctx.beginPath(); ctx.moveTo((px + 1) * T, y0 * T); ctx.lineTo((px + 1) * T, (y1 + 1) * T); ctx.stroke();
		}
	}
	for (const l of S.slots) {
		const x = l.x * T, y = l.y * T, inset = Math.max(1.5, T * 0.14);
		const filled = (l.qty || 0) > 0;
		if (filled) {
			const f = Math.min(1, l.qty / 60);
			ctx.fillStyle = hexA(C.rack, 0.3 + 0.6 * f);
			ctx.fillRect(x + inset, y + inset, T - inset * 2, T - inset * 2);
		}
		if (l.pick_face) {
			ctx.strokeStyle = filled ? '#2ECC71' : hexA('#2ECC71', 0.4); ctx.lineWidth = 1.5;
			ctx.strokeRect(x + 1, y + 1, T - 2, T - 2);
		}
		if (l.abc === 'A' && filled) { ctx.fillStyle = C.mark; ctx.fillRect(x + inset, y + inset, Math.max(2, T * 0.14), T - inset * 2); }
		ctx.strokeStyle = hexA(C.rack, 0.35); ctx.lineWidth = 1;
		ctx.beginPath(); ctx.moveTo(x, y + T); ctx.lineTo(x + T, y + T); ctx.stroke();
	}
	// aisle letters
	for (let k = 0; k < 9; k++) {
		const cx = (13 + 4 * k) * T;
		for (const yy of [3, 33]) {
			ctx.fillStyle = C.panel; ctx.strokeStyle = C.rack; ctx.lineWidth = 1;
			rr(cx - T * 0.55, (yy + 0.1) * T, T * 1.1, T * 0.8, 3); ctx.fill(); ctx.stroke();
			label(AISLES[k], cx, (yy + 0.52) * T, Math.max(9, T * 0.62), C.ink, 'center', 700);
		}
	}
	// staging spots with real receiving LPNs
	S.staging.forEach((s, i) => {
		const spot = SPOT_DEF[i]; if (!spot) return;
		ctx.strokeStyle = hexA(C.mark, 0.8); ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
		ctx.strokeRect(spot.x * T + 1.5, spot.y * T + 1.5, T - 3, T - 3); ctx.setLineDash([]);
		drawPallet((spot.x + 0.5) * T, (spot.y + 0.5) * T, T * 0.8, false);
		if (s.qty) label(String(Math.round(s.qty)), (spot.x + 0.5) * T, (spot.y + 0.95) * T, Math.max(7, T * 0.42), C.ink, 'center', 700, 'mono');
	});
	// QC bench, RMA desk, supervisor desk
	const bench = (x, y, w, h, txt, col) => { ctx.fillStyle = C.panel; ctx.strokeStyle = col; ctx.lineWidth = 1.5; rr(x * T + 1, y * T + 2, w * T - 2, h * T - 4, 3); ctx.fill(); ctx.stroke(); label(txt, (x + w / 2) * T, (y + h / 2) * T, Math.max(8, T * 0.55), col, 'center', 700); };
	bench(6, 25, 2, 1, 'QC BENCH', '#16A34A');
	bench(6, 31, 3, 1, 'RMA DESK', '#A16207');
	bench(49, 33, 2, 1, 'SUPV', '#E11D48');
	ctx.save(); ctx.beginPath(); ctx.rect(9 * T, 24 * T, 2 * T, 2 * T); ctx.clip();
	ctx.fillStyle = hexA(C.bad, 0.1); ctx.fillRect(9 * T, 24 * T, 2 * T, 2 * T);
	ctx.strokeStyle = hexA(C.bad, 0.45); ctx.lineWidth = 1;
	for (let i = -2; i < 5; i++) { ctx.beginPath(); ctx.moveTo((9 + i * 0.5) * T, 26 * T); ctx.lineTo((11 + i * 0.5) * T, 24 * T); ctx.stroke(); }
	ctx.restore();
	ctx.strokeStyle = C.bad; ctx.lineWidth = 1.5; ctx.strokeRect(9 * T + 1, 24 * T + 1, 2 * T - 2, 2 * T - 2);
	label(`QTN ${S.qcCount}`, 10 * T, 25 * T, Math.max(8, T * 0.55), C.bad, 'center', 700);
	// pack stations
	for (let i = 0; i < ST_Y.length; i++) {
		const st = ST_Y[i];
		const open = i < 4; // seeded packers
		ctx.fillStyle = open ? C.panel : C.panel2; ctx.strokeStyle = open ? '#DB2777' : C.line; ctx.lineWidth = 1.5;
		rr(49 * T + 1, st * T + 2, 2 * T - 2, T - 4, 3); ctx.fill(); ctx.stroke();
		label(`P${i + 1}${open ? '' : ' closed'}`, 50 * T, (st + 0.5) * T, Math.max(8, T * 0.55), open ? '#DB2777' : C.muted, 'center', 700);
	}
	// conveyor
	const belt = (x, y, w, h) => { ctx.fillStyle = '#26313B'; ctx.fillRect(x * T, y * T, w * T, h * T); };
	belt(53.15, 3, 0.7, 29);
	for (const st of ST_Y) belt(52, st + 0.15, 1.5, 0.7);
	ctx.strokeStyle = hexA('#ffffff', 0.12); ctx.lineWidth = 1;
	const off = (performance.now() / 500) % 0.5;
	for (let y = 3 + off; y < 32; y += 0.5) { ctx.beginPath(); ctx.moveTo(53.15 * T, y * T); ctx.lineTo(53.85 * T, y * T); ctx.stroke(); }
	// outbound lanes + real carton counts
	const perLane = [0, 1, 2].map(i => (i === 0 ? Math.min(S.laneCartons, 12) : 0));
	perLane[1] = Math.min(Math.max(0, S.laneCartons - perLane[0]), 12);
	for (let i = 0; i < OUT_DOOR_Y.length; i++) {
		const y = OUT_DOOR_Y[i];
		belt(54, y + 0.1, 6, 0.8);
		for (let x = 54; x < 60; x += 0.34) { ctx.beginPath(); ctx.moveTo(x * T, (y + 0.1) * T); ctx.lineTo(x * T, (y + 0.9) * T); ctx.stroke(); }
		const dock = S.docks.filter(d => d.direction === 'Outbound')[i];
		label(dock ? `${dock.door_code}${dock.trailer ? ' · ' + dock.trailer : ''}` : CARRIERS[i], 57 * T, (y - 0.35) * T, Math.max(8, T * 0.52), dock && dock.status !== 'Idle' ? '#FBBF24' : C.muted, 'center', 700);
		ctx.fillStyle = C.ink; ctx.fillRect(54 * T, (y - 0.2) * T, T * 0.35, T * 0.3);
		const n = perLane[i] || 0;
		for (let k = 0; k < n; k++) {
			const x = 59.55 - k * 0.5;
			ctx.fillStyle = KRAFT; ctx.strokeStyle = '#8A6A3E'; ctx.lineWidth = 1;
			ctx.fillRect((x - 0.2) * T, (y + 0.3) * T, T * 0.4, T * 0.4); ctx.strokeRect((x - 0.2) * T, (y + 0.3) * T, T * 0.4, T * 0.4);
		}
		// truck when the door is active
		if (dock && dock.status !== 'Idle') drawTruck(63.1, y, 1, `OUT ${i + 1}`, n / 12);
	}
	// inbound doors + truck when LPNs are being received
	const inDocks = S.docks.filter(d => d.direction === 'Inbound');
	IN_DOOR_Y.forEach((y, i) => {
		ctx.fillStyle = C.floor; ctx.fillRect(3.7 * T, y * T + 1, T * 0.6, T - 2);
		ctx.fillStyle = hexA(C.mark, 0.9); ctx.fillRect(3.85 * T, y * T + 2, T * 0.3, T - 4);
		const dock = inDocks[i];
		label(`D${i + 1}`, 4.55 * T, (y - 0.35) * T, Math.max(8, T * 0.5), C.muted, 'left', 700);
		if (dock && dock.status !== 'Idle') {
			label(`${dock.door_code}${dock.trailer ? ' · ' + dock.trailer : ''}`, 5.6 * T, (y + 0.5) * T, Math.max(7, T * 0.44), '#FBBF24', 'left', 700, 'mono');
			drawTruck(0.1, y, -1, 'IN', Math.min(1, S.inboundLpns / 8));
		}
	});

	// tour highlight
	if (UI.tourZone && ZONES[UI.tourZone]) {
		const z = ZONES[UI.tourZone];
		const a = 0.55 + 0.35 * Math.sin(performance.now() / 300);
		ctx.strokeStyle = hexA('#F2B705', a); ctx.lineWidth = 3; ctx.setLineDash([8, 5]);
		ctx.strokeRect(z.x * T + 2, z.y * T + 2, z.w * T - 4, z.h * T - 4); ctx.setLineDash([]);
	}

	// agents
	const sel = UI.selected != null ? S.agents.get(UI.selected) : null;
	if (sel && sel.path.length) {
		ctx.strokeStyle = hexA(ROLES[sel.role].color, 0.7); ctx.lineWidth = 2; ctx.setLineDash([4, 4]);
		ctx.beginPath(); ctx.moveTo((sel.x + 0.5) * T, (sel.y + 0.5) * T);
		for (const n of sel.path) ctx.lineTo((n % W + 0.5) * T, (((n / W) | 0) + 0.5) * T);
		ctx.stroke(); ctx.setLineDash([]);
		const g = sel.path[sel.path.length - 1];
		ctx.fillStyle = ROLES[sel.role].color; ctx.beginPath(); ctx.arc((g % W + 0.5) * T, (((g / W) | 0) + 0.5) * T, T * 0.15, 0, 7); ctx.fill();
	}
	for (const a of S.agents.values()) drawAgent(a, a === sel);
}

function drawTruck(x0, y, dir, tag, fill) {
	ctx.save();
	ctx.fillStyle = C.panel; ctx.strokeStyle = C.wall; ctx.lineWidth = 1.5;
	rr(x0 * T, (y - 0.45) * T, 2.8 * T, 1.9 * T, 3); ctx.fill(); ctx.stroke();
	ctx.fillStyle = C.wall; rr((dir < 0 ? x0 - 0.35 : x0 + 2.8) * T, (y - 0.1) * T, 0.35 * T, 1.2 * T, 2); ctx.fill();
	ctx.fillStyle = hexA(KRAFT, 0.85);
	ctx.fillRect((x0 + 0.15) * T, (y - 0.3) * T, 2.5 * Math.max(0.08, fill) * T, 1.2 * T);
	label(tag, (x0 + 1.4) * T, (y + 1.22) * T, Math.max(7, T * 0.45), C.muted, 'center', 600, 'mono');
	ctx.restore();
}

function drawAgent(a, selected) {
	const role = ROLES[a.role] || ROLES.forklift;
	const col = role.color;
	const cx = (a.x + 0.5) * T, cy = (a.y + 0.5) * T;
	const fx = a.face.x, fy = a.face.y;
	if (selected) { ctx.strokeStyle = C.ink; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, T * 0.62, 0, 7); ctx.stroke(); }
	// scanning pulse at task target
	if (a.task && onTile(a, a.task.to_xy || a.task.from_xy || [a.x, a.y])) {
		const p = 0.5 + 0.5 * Math.sin(performance.now() / 200);
		ctx.strokeStyle = hexA('#EF4444', 0.3 + 0.5 * p); ctx.lineWidth = 1.5;
		ctx.beginPath(); ctx.arc(cx, cy, T * (0.4 + 0.12 * p), 0, 7); ctx.stroke();
	}
	if (role.vehicle) {
		const horiz = fx !== 0;
		const w = horiz ? T * 0.86 : T * 0.62, h = horiz ? T * 0.62 : T * 0.86;
		ctx.fillStyle = col; ctx.strokeStyle = '#0B0F12'; ctx.lineWidth = 1.2;
		rr(cx - w / 2, cy - h / 2, w, h, 3); ctx.fill(); ctx.stroke();
	} else {
		const r = T * 0.34;
		if (a.role === 'supervisor') { ctx.strokeStyle = '#F2B705'; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(cx, cy, r + 2, 0, 7); ctx.stroke(); }
		ctx.fillStyle = col; ctx.strokeStyle = '#0B0F12'; ctx.lineWidth = 1.5;
		ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill(); ctx.stroke();
		ctx.fillStyle = '#0B0F12';
		ctx.beginPath(); ctx.arc(cx + fx * r * 0.45, cy + fy * r * 0.45, r * 0.28, 0, 7); ctx.fill();
	}
	const hx = cx + fx * T * 0.34, hy = cy + fy * T * 0.34;
	ctx.fillStyle = a.task ? '#EF4444' : '#E2E8ED';
	ctx.fillRect(hx - T * 0.08, hy - T * 0.08, T * 0.16, T * 0.16);
	// bubbles
	const show = selected || a.task && (UI.bubbles === 'all' || UI.bubbles === 'scans');
	if (show && UI.bubbles !== 'off' && a.bubble) drawBubble(a, a.bubble, col);
}

function drawBubble(a, text, col) {
	const fs = Math.max(10, Math.min(12.5, T * 0.72));
	ctx.font = `600 ${fs}px Barlow, "Segoe UI", sans-serif`;
	const tw = ctx.measureText(text).width;
	const pw = tw + 12, ph = fs + 8;
	let x = (a.x + 0.5) * T - pw / 2, y = (a.y + 0.5) * T - T * 0.6 - ph - 4;
	x = Math.max(2, Math.min(W * T - pw - 2, x)); if (y < 2) y = (a.y + 0.5) * T + T * 0.7;
	ctx.fillStyle = C.panel; ctx.strokeStyle = col; ctx.lineWidth = 1.5;
	rr(x, y, pw, ph, 4); ctx.fill(); ctx.stroke();
	ctx.fillStyle = C.ink; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
	ctx.fillText(text, x + 6, y + ph / 2 + 0.5);
}

/* ---------------------------------------------------------------- UI helpers */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clockOf = iso => String(iso || '').slice(11, 19);

function segment(id, key, cb) {
	const el = $(id);
	el.addEventListener('click', async e => {
		const b = e.target.closest('button'); if (!b) return;
		for (const x of el.querySelectorAll('button')) x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
		UI[key] = b.dataset.v;
		if (cb) await cb(b.dataset.v);
	});
}

function renderKpis(D) {
	const k = D.kpi || {};
	$('kShipped').textContent = k.orders_shipped_today ?? '–';
	$('kOpen').textContent = k.open_orders ?? '–';
	$('kCycle').textContent = k.avg_cycle_time_minutes != null ? `${Math.round(k.avg_cycle_time_minutes)} min` : '–';
	$('kLines').textContent = k.lines_picked_per_hour ?? '–';
	$('kScans').textContent = k.rf_scans_last_hour ?? '–';
	$('kPack').textContent = D.pack_queue ?? 0;
	const inUse = (D.docks || []).filter(d => d.status !== 'Idle').length;
	$('kDock').textContent = `${inUse}/${(D.docks || []).length}`;
	$('kWave').textContent = D.release_mode === 'Waves' ? 'Waves' : 'Continuous';
	$('kWaveS').textContent = D.release_mode === 'Waves' ? 'orders held for release' : 'each order released on arrival';
	if (D.shift) $('shiftname').textContent = `${D.shift.shift_name} shift`;
	// state bar
	const workers = D.workers || [];
	const busy = workers.filter(w => (D.task_by_worker || {})[w.name]).length;
	const idle = workers.length - busy;
	$('stateBar').innerHTML =
		`<i style="width:${workers.length ? 100 * busy / workers.length : 0}%;background:#16A34A" title="On task ${busy}"></i>` +
		`<i style="width:${workers.length ? 100 * idle / workers.length : 0}%;background:#94A3B8" title="Idle ${idle}"></i>`;
	$('stateKey').innerHTML =
		`<span><i class="sq" style="background:#16A34A"></i> On task <b>${busy}</b></span>` +
		`<span><i class="sq" style="background:#94A3B8"></i> Idle <b>${idle}</b></span>` +
		`<span><i class="sq" style="background:#2563EB"></i> ${workers.length} workers signed in</span>`;
}

/* ---------------------------------------------------------------- event log */
function evRow(e) {
	const st = STAGES[e.category] || { code: (e.category || 'EV').slice(0, 6).toUpperCase(), color: '#91A1AF' };
	const who = e.worker ? e.worker : 'WMS';
	return `<li class="ev${e.severity === 'Alert' ? ' alert' : ''}" style="--c:${st.color}">` +
		`<time>${esc(clockOf(e.creation))}</time>` +
		`<div class="l1"><code>${esc(e.subtype || st.code)}</code>${e.is_scan ? '<span class="rf">RF</span>' : ''}<span class="by">${esc(who)}</span></div>` +
		`<div class="msg">${esc(e.message)}</div></li>`;
}
function buildLog(D) {
	seenEvents = new Set((D.events || []).map(e => e.name));
	$('evlist').innerHTML = (D.events || []).slice(0, 150).map(evRow).join('');
	$('logFilter').innerHTML = (D.filters || []).map(f => `<option value="${esc(f.key)}">${esc(f.label)}</option>`).join('');
}
function mergeLog(events) {
	const fresh = events.filter(e => !seenEvents.has(e.name));
	if (!fresh.length) return;
	for (const e of fresh) seenEvents.add(e.name);
	const f = FILTERS[UI.filter] || FILTERS.all;
	const rows = fresh.filter(f).map(evRow).join('');
	if (rows) {
		const list = $('evlist');
		list.insertAdjacentHTML('afterbegin', rows);
		while (list.children.length > 150) list.lastElementChild.remove();
	}
}

/* ---------------------------------------------------------------- inspector */
function renderInspector() {
	const a = UI.selected != null ? S.agents.get(UI.selected) : null;
	let html = '';
	if (!a) {
		html = `<p class="hint">Click any worker on the floor, or pick one below, to see what they are doing and why.</p>`;
	} else {
		const role = ROLES[a.role] || ROLES.forklift;
		const t = a.task;
		const now = t ? `${t.task_type}${t.item ? ' · ' + t.item : ''}${t.qty ? ' × ' + t.qty : ''}${t.to_bin ? ' → ' + t.to_bin : ''}` : 'Waiting for work';
		html = `
			<div class="who"><i class="av" style="background:${role.color}"></i><div><h3>${esc(a.first)} · ${esc(a.code)}</h3>
				<p>${esc(role.label)} · RF handheld · ${t ? 'On task' : 'Idle'}</p></div></div>
			<div class="now"><div class="lab">Now</div><div class="txt">${esc(now)}</div></div>
			<div class="why"><em>Why this task</em>${esc(t ? (TASK_WHY[t.task_type] || t.task_type) : 'No open task in the queue for this role right now.')}</div>
			${t ? `<div class="why"><em>Reference</em>${esc(`${t.name}${t.wave ? ' · ' + t.wave : ''}${t.lpn ? ' · ' + t.lpn : ''}`)}</div>` : ''}
			<div class="stats"><div><b>${a.scans}</b><span>scans today</span></div><div><b>${a.tasks}</b><span>tasks done</span></div><div><b>${a.task ? 'busy' : 'idle'}</b><span>state</span></div></div>
			${recentScans(a.worker)}`;
	}
	const roster = [...S.agents.values()].map(w =>
		`<button type="button" data-id="${esc(w.worker)}" aria-pressed="${w.worker === UI.selected ? 'true' : 'false'}"><i class="dot" style="background:${(ROLES[w.role] || ROLES.forklift).color}"></i>${esc(w.code)}</button>`
	).join('');
	$('insp').innerHTML = html + `<div class="roster">${roster}</div>`;
	$('btnFollow').hidden = UI.selected == null;
}
function recentScans(worker) {
	const evs = ((S.data || {}).events || []).filter(e => e.worker === worker && e.is_scan).slice(0, 6);
	if (!evs.length) return '<p class="hint">No scans yet.</p>';
	return `<div><div class="why"><em>Recent scans</em></div><ul class="scanlist">${evs.map(e =>
		`<li><code>${esc(clockOf(e.creation))}</code><span><b>${esc(e.subtype || '')}</b> ${esc(e.message)}</span></li>`).join('')}</ul></div>`;
}
$('insp').addEventListener('click', e => {
	const b = e.target.closest('.roster button'); if (!b) return;
	UI.selected = UI.selected === b.dataset.id ? null : b.dataset.id;
	renderInspector();
});
$('btnFollow').onclick = () => { UI.selected = null; renderInspector(); };

// canvas picking
function agentAt(ev) {
	const r = cv.getBoundingClientRect();
	const x = (ev.clientX - r.left) / T - 0.5, y = (ev.clientY - r.top) / T - 0.5;
	let best = null, bd = 0.9;
	for (const a of S.agents.values()) { const d = Math.hypot(a.x - x, a.y - y); if (d < bd) { bd = d; best = a; } }
	return best;
}
cv.addEventListener('click', ev => {
	const a = agentAt(ev);
	UI.selected = a ? a.worker : null;
	renderInspector();
});
cv.addEventListener('mousemove', ev => { cv.style.cursor = agentAt(ev) ? 'pointer' : 'default'; });

/* ---------------------------------------------------------------- controls */
segment('speedSeg', 'speed');
segment('bubbleSeg', 'bubbles');
segment('modeSeg', 'releaseMode', async v => {
	try {
		await api('fwms.api.floorview.set_release_mode', { mode: v });
		$('kWave').textContent = v;
		$('kWaveS').textContent = v === 'Waves' ? 'orders held for release' : 'each order released on arrival';
	} catch (e) { console.warn(e); }
});
$('btnPlay').onclick = () => { UI.playing = !UI.playing; $('btnPlay').textContent = UI.playing ? 'Pause' : 'Play'; };
$('btnTruck').onclick = async () => { $('btnTruck').disabled = true; try { await api('fwms.api.floorview.simulate_truck'); poll(); } catch (e) { console.warn(e); } $('btnTruck').disabled = false; };
$('btnSpike').onclick = async () => {
	$('btnSpike').disabled = true; $('btnSpike').textContent = 'Creating…';
	try { await api('fwms.api.floorview.simulate_orders', { count: 20 }); poll(); }
	catch (e) { console.warn(e); }
	$('btnSpike').textContent = '+ 20 orders'; $('btnSpike').disabled = false;
};
$('btnReset').onclick = () => location.reload();
document.addEventListener('keydown', e => {
	if (e.code === 'Space' && !/INPUT|SELECT|BUTTON|TEXTAREA/.test(document.activeElement.tagName)) {
		e.preventDefault(); $('btnPlay').click();
	}
});
$('legend').innerHTML = Object.values(ROLES).map(r => `<span><i class="dot" style="background:${r.color}"></i>${r.label}</span>`).join('') +
	`<span><i class="sq" style="background:#6F95B8"></i>Stocked slot</span><span><i class="sq" style="background:#F2B705"></i>A-class</span><span><i class="sq" style="background:#2ECC71"></i>Pick face</span>`;
$('logFilter').addEventListener('change', e => {
	UI.filter = e.target.value;
	if (S.data) buildLog(S.data), UI.booted = true;
});

/* ---------------------------------------------------------------- tour */
const TOUR = [
	{ t: 'Trailer check-in and receiving', z: 'receiving', role: 'receiver', f: 'Receiving & QC',
	  b: 'An inbound trailer docks at a door. The receiver opens the receipt by scanning the Purchase Order, scans each item and quantity into an LPN, and records the QC decision — passes go to staging, failures route to QC Hold with a Quality Inspection.',
	  e: 'scan:receive → RECEIVE (PR + LPN) → Putaway task queued' },
	{ t: 'Quality inspection hold', z: 'qc', role: 'qc', f: 'Receiving & QC',
	  b: 'Failed receipts land in the QC Hold warehouse with a Rejected Quality Inspection. Inventory Control reviews and either releases stock back into play or scraps it. Rejected quantities never reach pickable pick faces.',
	  e: 'Quality Inspection (Rejected) → QC Hold - FW' },
	{ t: 'System-directed putaway', z: 'storage', role: 'forklift', f: 'Putaway & Storage',
	  b: 'Putaway tasks queue the moment a receipt commits. The WMS suggests the bin: a pick face slotted for the item and below its max, otherwise bulk storage. Scanning the LPN and the bin confirms the move — crossing zones posts a real Stock Entry.',
	  e: 'Putaway task → suggest_bin → confirm → Stock Entry (zone change)' },
	{ t: 'Order release: continuous or waves', z: 'orders', role: 'picker', f: 'Orders & Picking',
	  b: 'Submitting a Sales Order in Continuous mode releases it instantly: a wave, an FEFO-ordered Pick List built from real pick-face stock, and one directed pick task per bin visit. Switch to Waves to hold orders for batched release. Try + 20 orders to see a spike.',
	  e: 'SO submit → FWMS Wave → Pick List (FEFO) → Pick tasks' },
	{ t: 'Directed picking with FEFO batches', z: 'storage', role: 'picker', f: 'Orders & Picking',
	  b: 'Pickers claim the next task themselves, scan the cart, the bin and the item. The WMS offers the earliest-expiry batch and validates serials against the source LPN. When the wave’s last pick lands, the Pick List submits and a draft Delivery Note appears.',
	  e: 'claim → scan bin/item → FEFO batch + serials → Pick List submit → draft DN' },
	{ t: 'Pack and verify', z: 'pack', role: 'packer', f: 'Packing',
	  b: 'Finished carts enter the pack queue. Packing validates cart contents against the Delivery Note, posts a native Packing Slip, and prints a ZPL carton label — downloaded, or pushed raw to a network printer on port 9100.',
	  e: 'pack queue → Packing Slip → ZPL label → carton Packed' },
	{ t: 'Sortation and trailer loading', z: 'outbound', role: 'loader', f: 'Sort Load & Ship',
	  b: 'Packed cartons are scanned to an outbound dock door and staged for the trailer. The dock flips to Loading and the lane fills on the right — that count is real packed cartons waiting to ship.',
	  e: 'load(carton, dock, trailer) → dock Loading' },
	{ t: 'Ship confirm and cycle time', z: 'outbound', role: 'loader', f: 'Sort Load & Ship',
	  b: 'Closing the trailer submits the Delivery Notes — stock leaves the Pick Faces warehouse in the real ledger, LPNs flip to Shipped, and the wave closes. Release-to-trailer time feeds the cycle-time KPI (last 30 orders).',
	  e: 'ship(dock) → DN submit → wave Closed → KPI cycle time' },
	{ t: 'Cycle counting', z: 'storage', role: 'inventory', f: 'Inventory Control',
	  b: 'Count Programs schedule bins by ABC class — A items monthly, C items twice a year. Counts are blind by default; a variance raises a draft Stock Reconciliation for Inventory Control to approve, and the bin’s accuracy is tracked.',
	  e: 'Count task → blind count → SR draft → approve → ledger adjusted' },
	{ t: 'Customer returns', z: 'returns', role: 'returns', f: 'Returns',
	  b: 'Scan the original Delivery Note, scan the item back, and grade it: A restocks to the pick faces, B goes to QC hold, Refurb to bulk, Scrap to the scrap warehouse. Each grade drafts the matching Sales Return.',
	  e: 'scan DN → grade → Sales Return drafts per grade' },
	{ t: 'Supervisor and exceptions', z: 'all', role: 'supervisor', f: 'Alerts & Supervisor',
	  b: 'Shortages, backpressure and release-mode changes land in Alerts & Supervisor. Every event — including this dashboard’s own actions — flows through the same FWMS event log that feeds the RF terminal and KPIs. Enable Simulation Mode to inject synthetic floor activity for demos.',
	  e: 'shortage / alerts / release_mode / simulate_tick' },
];
$('pips').innerHTML = TOUR.map((s, i) => `<button type="button" data-i="${i}" aria-label="Step ${i + 1}: ${esc(s.t)}"></button>`).join('');
$('pips').onclick = e => { const b = e.target.closest('button'); if (b) applyTour(+b.dataset.i); };
$('tourPrev').onclick = () => applyTour((UI.tour + TOUR.length - 1) % TOUR.length);
$('tourNext').onclick = () => applyTour((UI.tour + 1) % TOUR.length);
function applyTour(i) {
	UI.tour = i; const s = TOUR[i];
	$('tourN').textContent = `${i + 1} / ${TOUR.length}`;
	$('tourTitle').textContent = s.t; $('tourBody').textContent = s.b; $('tourEng').textContent = 'Events: ' + s.e;
	for (const b of $('pips').children) b.setAttribute('aria-current', +b.dataset.i === i ? 'true' : 'false');
	UI.tourZone = s.z;
	UI.filter = s.f; $('logFilter').value = s.f;
	if (S.data) buildLog(S.data), UI.booted = true;
	// select a worker with that role
	for (const a of S.agents.values()) if (a.role === s.role) { UI.selected = a.worker; break; }
	renderInspector();
}

/* ---------------------------------------------------------------- loop */
let last = performance.now();
function frame(now) {
	const dt = Math.min(0.1, (now - last) / 1000); last = now;
	if (UI.playing) for (const a of S.agents.values()) moveAgent(a, dt);
	// wall clock
	const d = new Date();
	$('clock').textContent = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
	draw();
	requestAnimationFrame(frame);
}

new ResizeObserver(resize).observe(wrap);
resize();
poll();
pollTimer = setInterval(poll, 2000);
requestAnimationFrame(frame);
})();
