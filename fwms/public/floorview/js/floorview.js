/* FWMS live floor view — WMS-App style dashboard.
 * Polls fwms.api.floorview.dashboard every 2s; upgrades to realtime
 * (fwms_event / fwms_kpi) when frappe realtime is reachable. */

window.FV = (() => {
	const state = { data: null, filter: 'all', labelMode: 'all' };
	const $ = (id) => document.getElementById(id);

	async function api(method, params = {}) {
		const body = new URLSearchParams(params);
		const r = await fetch(`/api/method/${method}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json' },
			body,
		});
		const d = await r.json();
		if (!r.ok || d.exc) throw new Error(String(d.exc || r.status));
		return d.message;
	}

	function refresh() {
		return api('fwms.api.floorview.dashboard').then(render).catch(() => {});
	}

	function render(data) {
		state.data = data;
		renderKpis(data.kpi);
		renderZones(data);
		renderDocks(data.docks);
		renderWorkers(data.workers);
		renderFilters(data.filters);
		if (!state.eventsPreloaded) { renderEvents(data.events); state.eventsPreloaded = true; }
		$('release-mode').textContent = `Release: ${data.release_mode}`;
		if (data.simulation_mode) $('sim-btn').classList.remove('hidden');
		if (data.shift) $('shiftname').textContent = `${data.shift.shift_name} shift`;
	}

	function renderKpis(kpi) {
		const cards = [
			['Orders shipped', kpi?.orders_shipped_today ?? '—'],
			['Open orders', kpi?.open_orders ?? '—'],
			['Cycle time (min)', kpi?.avg_cycle_time_minutes ?? '—'],
			['Lines / hr', kpi?.lines_picked_per_hour ?? '—'],
			['RF scans', kpi?.rf_scans_last_hour ?? '—'],
			['Pack queue', kpi?.pack_queue ?? '—'],
			['Docks in use', kpi?.dock_doors_in_use ?? '—'],
			['Count accuracy', kpi?.count_accuracy_pct != null ? kpi.count_accuracy_pct + '%' : '—'],
		];
		$('kpis').replaceChildren(...cards.map(([l, v]) => {
			const d = document.createElement('div'); d.className = 'kpi';
			const vv = document.createElement('div'); vv.className = 'v'; vv.textContent = v;
			const ll = document.createElement('div'); ll.className = 'l'; ll.textContent = l;
			d.append(vv, ll); return d;
		}));
	}

	function renderZones(data) {
		const map = $('zone-map');
		map.replaceChildren();
		const zoneWorkers = {};
		for (const w of data.workers || []) {
			if (w.default_zone) zoneWorkers[w.default_zone] = (zoneWorkers[w.default_zone] || 0) + 1;
		}
		const zoneTasks = {};
		for (const t of data.open_tasks || []) {
			const zone = (t.zone || '').trim();
			if (zone) zoneTasks[zone] = (zoneTasks[zone] || 0) + 1;
		}
		for (const z of data.zones || []) {
			const d = document.createElement('div');
			d.className = 'zone' + (zoneTasks[z.zone_name] ? ' hot' : '');
			d.dataset.zone = z.zone_name;
			d.style.left = (z.map_x || 0) + '%';
			d.style.top = (z.map_y || 0) + '%';
			d.style.width = (z.map_w || 20) + '%';
			d.style.height = (z.map_h || 20) + '%';
			d.style.background = (z.color || '#333') + '22';
			const nm = document.createElement('div'); nm.className = 'zname'; nm.textContent = z.zone_name;
			const ct = document.createElement('div'); ct.className = 'zcount';
			ct.textContent = `${zoneWorkers[z.zone_name] || 0} workers · ${zoneTasks[z.zone_name] || 0} open tasks`;
			d.append(nm, ct);
			map.append(d);
		}
	}

	function renderDocks(docks) {
		const wrap = $('docks');
		wrap.replaceChildren(...(docks || []).map((d) => {
			const el = document.createElement('div');
			el.className = `dock ${d.status}`;
			el.dataset.dock = d.door_code;
			const code = document.createElement('div'); code.className = 'code'; code.textContent = d.door_code;
			const st = document.createElement('div'); st.className = 'st';
			st.textContent = `${d.direction} · ${d.status}${d.trailer ? ' · ' + d.trailer : ''}`;
			el.append(code, st);
			return el;
		}));
	}

	function renderWorkers(workers) {
		const wrap = $('workers');
		$('worker-count').textContent = `· ${workers.length}`;
		wrap.replaceChildren(...workers.map((w) => {
			const el = document.createElement('div');
			el.className = 'worker' + (w.current_task ? ' busy' : '');
			el.dataset.worker = w.worker_code;
			const dot = document.createElement('span'); dot.className = 'dot';
			const nm = document.createElement('span');
			nm.textContent = `${w.worker_code} ${w.person_name.split(' ')[0]}`;
			const tsk = document.createElement('span'); tsk.className = 'sub';
			tsk.textContent = w.current_task ? '● task' : `${w.scans_today || 0} scans`;
			el.append(dot, nm, tsk);
			return el;
		}));
	}

	function renderFilters(filters) {
		if (state.filtersBuilt) return;
		state.filtersBuilt = true;
		const wrap = $('filters');
		wrap.replaceChildren(...filters.map((f) => {
			const b = document.createElement('button');
			b.className = 'fbtn' + (f.key === state.filter ? ' on' : '');
			b.textContent = f.label;
			b.onclick = () => {
				state.filter = f.key;
				wrap.querySelectorAll('.fbtn').forEach((x) => x.classList.remove('on'));
				b.classList.add('on');
				renderEvents(state.data?.events || []);
			};
			return b;
		}));
		document.querySelectorAll('#label-toggle button').forEach((b) => {
			b.onclick = () => {
				state.labelMode = b.dataset.mode;
				document.querySelectorAll('#label-toggle button').forEach((x) => x.classList.remove('on'));
				b.classList.add('on');
				renderEvents(state.data?.events || []);
			};
		});
	}

	function matchFilter(ev) {
		if (state.filter === 'all') return true;
		if (state.filter === 'scans') return !!ev.is_scan;
		return ev.category === state.filter;
	}

	function eventNode(ev) {
		const d = document.createElement('div');
		d.className = `event cat-${(ev.category || '').split(' ')[0]} sev-${ev.severity || 'Info'}${ev.is_scan ? ' scan' : ''}`;
		const meta = document.createElement('div'); meta.className = 'meta';
		const when = (ev.creation || '').slice(11, 19);
		meta.textContent = `${when} · ${ev.category}${ev.worker ? ' · ' + ev.worker : ''}${ev.zone ? ' · ' + ev.zone : ''}`;
		const msg = document.createElement('div'); msg.className = 'msg'; msg.textContent = ev.message;
		d.append(meta, msg);
		return d;
	}

	function renderEvents(events) {
		const wrap = $('events');
		if (state.labelMode === 'off') { wrap.replaceChildren(); return; }
		const shown = (events || []).filter((ev) =>
			matchFilter(ev) && (state.labelMode !== 'scans' || ev.is_scan));
		wrap.replaceChildren(...shown.map(eventNode));
	}

	function pushEvent(ev) {
		// realtime event arrives -> prepend if matching filter
		const data = state.data || (state.data = { events: [] });
		data.events = [ev, ...(data.events || [])].slice(0, 120);
		if (matchFilter(ev) && (state.labelMode !== 'scans' || ev.is_scan) && state.labelMode !== 'off') {
			$('events').prepend(eventNode(ev));
		}
		// highlight the zone briefly
		if (ev.zone) {
			const z = document.querySelector(`.zone[data-zone="${ev.zone}"]`);
			if (z) { z.classList.add('highlight'); setTimeout(() => z.classList.remove('highlight'), 2500); }
		}
	}

	// ------------------------------------------------ tour

	const TOUR = [
		['Welcome to the floor', 'This dashboard mirrors the warehouse in realtime. Every RF scan, document and alert lands here.'],
		['KPI cards', 'Top row tracks orders shipped, open orders, order cycle time (release → trailer, last 30), lines/hr and more.'],
		['Release mode', 'Continuous releases each order as it is confirmed; Waves batch by cutoff, route and dock door.'],
		['The zone map', 'Each block is a zone — Receiving, QC, Bulk, Pick Faces, Packing, Shipping. A glowing edge means open tasks there.'],
		['Dock doors', 'Inbound and outbound doors flip between Idle, Busy and Loading as trucks arrive and trailers close.'],
		['Workers on the floor', '26 workers self-assign tasks from live floor state. Busy workers pulse green with their current task.'],
		['Live event stream', 'Every action flows here: Receiving & QC, Putaway & storage, Orders & picking, Packing, Sort/load/ship, Inventory control, Returns, Alerts.'],
		['Handheld scans', 'Filter to "Handheld scans only" to see the raw RF confirmation behind every step.'],
		['Cycle counting', 'ABC-based count programs schedule bins; accuracy and variance land in the KPI row and Inventory control feed.'],
		['Simulation mode', 'Toggle Simulation in FWMS Settings and use ⚡ Simulate to generate synthetic floor events for demos.'],
		['Your turn', 'Open /rf on a handheld (or another browser tab), sign in with a worker PIN, and run a receipt — watch this board react.'],
	];

	const tour = {
		step: 0,
		start() { this.step = 0; $('tour-overlay').classList.remove('hidden'); this.show(); },
		stop() { $('tour-overlay').classList.add('hidden'); },
		next() { this.step = Math.min(this.step + 1, TOUR.length - 1); this.show(); },
		prev() { this.step = Math.max(this.step - 1, 0); this.show(); },
		show() {
			$('tour-n').textContent = this.step + 1;
			$('tour-title').textContent = TOUR[this.step][0];
			$('tour-body').textContent = TOUR[this.step][1];
			// spotlight matching region
			document.querySelectorAll('.zone,.dock,.kpi,.worker').forEach((x) => x.classList.remove('highlight'));
			const focus = [
				() => null,
				() => $('kpis'), () => $('release-mode'), () => $('zone-map'),
				() => $('docks'), () => $('workers'), () => $('events'), () => $('events'),
				() => $('kpis'), () => $('sim-btn'), () => null,
			][this.step];
			if (focus && focus()) focus().scrollIntoView({ block: 'center', behavior: 'smooth' });
		},
	};

	// ------------------------------------------------ boot

	function tickClock() {
		const now = new Date();
		$('clock').textContent = now.toTimeString().slice(0, 5);
	}
	setInterval(tickClock, 1000); tickClock();
	refresh();
	setInterval(refresh, 2000);

	// realtime upgrade if frappe socketio is available on this page
	if (window.frappe?.realtime) {
		frappe.realtime.on('fwms_event', pushEvent);
		frappe.realtime.on('fwms_kpi', (kpi) => renderKpis(kpi));
	}

	async function simTick() {
		try { await api('fwms.api.floorview.simulate_tick'); } catch (e) { console.warn(e); }
	}

	return { refresh, tour, simTick };
})();
