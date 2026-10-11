/* FWMS RF terminal — vanilla JS PWA.
 * Hash-routed screens, keyboard-wedge scan inputs, JSON-RPC to whitelisted
 * fwms.api.* endpoints. No build step. */

// ---------------------------------------------------------------- plumbing

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
	const body = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) body.append(k, v === undefined ? '' : v);
	const headers = {
		'Content-Type': 'application/x-www-form-urlencoded',
		'Accept': 'application/json',
	};
	if (CSRF) headers['X-Frappe-CSRF-Token'] = CSRF;
	const resp = await fetch(`/api/method/${method}`, { method: 'POST', headers, body });
	const data = await resp.json().catch(() => ({}));
	// session rotated (login/logout) -> stale token: refetch once and retry
	if (data.exc_type === 'CSRFTokenError' || /Invalid Request/.test(data.exc || '')) {
		if (_retry) {
			CSRF = null;
			await ensureCsrf(true);
			return api(method, params, false);
		}
	}
	if (!resp.ok || data.exc) {
		const msg = (data._server_messages && JSON.stringify(data._server_messages).slice(0, 220)) ||
			(data.exc || '').toString().split('\n').filter(Boolean).pop() || `HTTP ${resp.status}`;
		let readable = msg;
		try {
			readable = JSON.parse(JSON.parse(data._server_messages)[0]).message || readable;
		} catch (e) { /* keep raw */ }
		throw new Error(String(readable).replace(/<[^>]+>/g, '').slice(0, 200));
	}
	return data.message;
}

function el(tag, attrs = {}, ...children) {
	const node = document.createElement(tag);
	for (const [k, v] of Object.entries(attrs || {})) {
		if (k === 'class') node.className = v;
		else if (k === 'onclick') node.addEventListener('click', v);
		else if (k === 'oninput') node.addEventListener('input', v);
		else if (k === 'onenter') {
			node.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); v(e); } });
		} else if (v !== null && v !== undefined) node.setAttribute(k, v);
	}
	for (const child of children.flat()) {
		if (child === null || child === undefined) continue;
		node.append(child.nodeType ? child : document.createTextNode(child));
	}
	return node;
}

function toast(message, kind = '') {
	const t = el('div', { class: `toast ${kind}` }, message);
	document.body.append(t);
	setTimeout(() => t.remove(), 2600);
	if (kind === 'err') beep(220, 160); else beep(950, 70);
}

// state kept across screens in this shift session
const S = {
	worker: JSON.parse(localStorage.getItem('fwms.worker') || 'null'),
	receive: { po: null, lines: [], lpn: null },
	pick: { task: null, cart: null },
	count: { bin: null },
	returns: { dn: null, lines: [] },
	pack: { cart: null, dn: null },
	load: { dock: null },
	quality: { task: null, results: [] },
	scrap: { lpn: null, info: null, lines: [], reason: 'Damaged', held: null },
};

// ---------------------------------------------------------------- components

// scan card: a card containing one autofocus scan input wired to onScan(value, input)
function scanCard(label, placeholder, onScan, value = '') {
	const input = el('input', {
		type: 'text', autocomplete: 'off', autocapitalize: 'characters',
		placeholder: placeholder || 'Scan…', value: value || '',
	});
	input.addEventListener('focus', () => input.select());
	input.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') { e.preventDefault(); onScan(input.value.trim(), input); }
	});
	const card = el('div', { class: 'card' }, el('label', { class: 'small', style: 'text-transform:uppercase;letter-spacing:.06em' }, label), input);
	card.focusScan = () => { input.focus(); input.select(); };
	card.setLabel = (t) => { label = t; };
	return card;
}

// suggest card: scan input with a dropdown of system suggestions.
// suggest: {context: 'po'|'bin'|'lpn'|'item'|'dock'|'dn'|'cart'|'packed_cart'}
//        or {items: [{value,label,desc}]}, optional {filter: fn(rows)->rows}
function suggestCard(label, placeholder, onScan, value = '', suggest = null) {
	const card = scanCard(label, placeholder, onScan, value);
	if (!suggest) return card;
	const input = card.querySelector('input');
	const list = el('div', { class: 'suggest-list hidden' });
	card.style.position = 'relative';
	card.append(list);

	let rows = [], hi = -1, deb = null;
	const hide = () => { list.classList.add('hidden'); hi = -1; };
	const renderList = () => {
		list.replaceChildren(...rows.map((r, i) => el('div', {
			class: `suggest-item ${i === hi ? 'hi' : ''}`,
			onmousedown: (e) => { e.preventDefault(); pick(r); },
		},
			el('div', { class: 'suggest-label mono' }, r.label),
			r.desc ? el('div', { class: 'suggest-desc' }, r.desc) : null,
		)));
		list.classList.toggle('hidden', !rows.length);
	};
	const pick = (r) => { hide(); input.value = r.value; onScan(r.value, input); };
	const load = async () => {
		const txt = input.value.trim();
		try {
			if (suggest.items) {
				rows = suggest.items.filter((r) => !txt || r.value.toLowerCase().includes(txt.toLowerCase()));
			} else {
				rows = await api('fwms.api.rf_suggest.suggest', { context: suggest.context, txt });
			}
			if (suggest.filter) rows = rows.filter(suggest.filter);
		} catch (e) { rows = []; }
		hi = -1; renderList();
	};
	input.addEventListener('input', () => {
		clearTimeout(deb);
		deb = setTimeout(load, suggest.items ? 0 : 220);
	});
	input.addEventListener('focus', () => load());
	input.addEventListener('keydown', (e) => {
		if (list.classList.contains('hidden') || !rows.length) return;
		if (e.key === 'ArrowDown') { e.preventDefault(); hi = (hi + 1) % rows.length; renderList(); }
		else if (e.key === 'ArrowUp') { e.preventDefault(); hi = (hi - 1 + rows.length) % rows.length; renderList(); }
	});
	// Enter: a highlighted suggestion wins over the typed text
	input.addEventListener('keydown', (e) => {
		if (e.key === 'Enter' && hi >= 0 && rows[hi]) { e.preventDefault(); e.stopImmediatePropagation(); pick(rows[hi]); }
		else if (e.key === 'Escape') hide();
	});
	input.addEventListener('blur', () => setTimeout(hide, 180));
	return card;
}

function menuButton(label, ico, fn) {
	return el('div', { class: 'tile', onclick: fn }, el('div', { class: 'ico' }, ico), el('div', { class: 'lbl' }, label));
}

function row(title, sub, right) {
	return el('div', { class: 'rowline' },
		el('div', { class: 'grow' },
			el('div', { class: 'title' }, title),
			sub ? el('div', { class: 'sub' }, sub) : null,
		),
		right || null,
	);
}

function qtyButtons(input, step = 1) {
	const wrap = el('div', { class: 'rowline', style: 'border:0;padding:0' });
	const mk = (label, delta) => el('button', {
		class: 'pill', style: 'font-size:1rem;padding:8px 14px;cursor:pointer',
		onclick: () => { input.value = String(Math.max(0, (parseFloat(input.value) || 0) + delta)); input.dispatchEvent(new Event('input')); },
	}, label);
	return el('div', { style: 'display:flex;gap:8px' }, mk('−5', -5), mk('−1', -1), mk('+1', 1), mk('+5', 5));
}

// ---------------------------------------------------------------- shell

const app = document.getElementById('app');

function topbar(subtitle) {
	return el('div', { class: 'topbar' },
		el('div', { class: 'who' },
			el('div', { class: 'name' }, S.worker ? `${S.worker.worker_code} · ${S.worker.person_name}` : 'FWMS RF'),
			el('div', { class: 'sub' }, subtitle || ''),
		),
		el('button', {
			class: 'home-btn', title: 'Home', 'aria-label': 'Home',
			onclick: () => { location.hash = '#menu'; },
		},
			homeIco(),
		),
		el('button', {
			class: 'logout-btn',
			onclick: async () => {
				try { await api('fwms.api.rf_auth.logout'); } catch (e) { /* ignore */ }
				localStorage.removeItem('fwms.worker');
				S.worker = null;
				CSRF = null; // session rotated on logout
				location.hash = '#login';
			},
		}, 'Logout'),
	);
}

const HOME_SVG = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10.5V20h13v-9.5"/><path d="M10 20v-5.5h4V20"/></svg>';

// el() inserts strings as text nodes, so the icon's markup must go in via
// innerHTML — otherwise the SVG source renders as literal text.
function homeIco() {
	const span = el('span', { class: 'home-ico', 'aria-hidden': 'true' });
	span.innerHTML = HOME_SVG;
	return span;
}

function render(topbarEl, ...body) {
	app.replaceChildren();
	const screen = el('div', { class: 'screen' }, ...body);
	app.append(topbarEl || '', screen);
}

function go(hash) { location.hash = hash; }

// ---------------------------------------------------------------- screens

function screenLogin() {
	app.replaceChildren(el('div', { class: 'screen', style: 'justify-content:center;max-width:420px;margin:0 auto;width:100%' },
		el('div', { style: 'text-align:center;margin-bottom:18px' },
			el('div', { style: 'font-size:2.4rem;font-weight:800' }, 'FWMS'),
			el('div', { class: 'small' }, 'Warehouse RF Terminal'),
		),
		(() => {
			const code = el('input', { type: 'text', placeholder: 'Worker code (e.g. W001)', autocomplete: 'off', style: 'text-transform:uppercase' });
			const pin = el('input', { type: 'password', placeholder: 'PIN', inputmode: 'numeric' });
			code.onenter = pin; // noop
			const btn = el('button', { class: 'primary', onclick: doLogin }, 'Sign in');
			async function doLogin() {
				try {
					const me = await api('fwms.api.rf_auth.login', { worker_code: code.value.trim(), pin: pin.value.trim() });
					S.worker = me;
					localStorage.setItem('fwms.worker', JSON.stringify(me));
					CSRF = null; // session rotated on login; refetch on next call
					toast(`Signed in: ${me.person_name}`, 'ok');
					go('#menu');
				} catch (e) { toast(e.message, 'err'); pin.value = ''; }
			}
			pin.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
			setTimeout(() => code.focus(), 50);
			return el('div', { class: 'card' },
				el('div', { class: 'scanbox' }, el('label', {}, 'Worker'), code),
				el('div', { class: 'scanbox' }, el('label', {}, 'PIN'), pin),
				btn,
			);
		})(),
	));
}

async function screenMenu() {
	render(topbar('Operations'), el('div', { class: 'menu' },
		menuButton('Receiving', '📥', () => go('#receive')),
		menuButton('Putaway', '📦', () => go('#putaway')),
		menuButton('Picking', '🛒', () => go('#pick')),
		menuButton('Packing', '📦', () => go('#pack')),
		menuButton('Count', '🔢', () => go('#count')),
		menuButton('Replenish', '♻️', () => go('#replenish')),
		menuButton('Load / Ship', '🚛', () => go('#loadship')),
		menuButton('Returns', '↩️', () => go('#returns')),
		menuButton('Quality', '🧪', () => go('#quality')),
		menuButton('Scrap', '🗑️', () => go('#scrap')),
	));
}

// ------------------------------------------------ receive

async function screenReceive() {
	const st = S.receive;
	const body = [];

	if (!st.po) {
		const scan = suggestCard('Scan Purchase Order', 'PO-…', async (val, input) => {
			try {
				const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'receive' });
				if (res.type !== 'po') throw new Error(`Not a Purchase Order (${res.type})`);
				const info = await api('fwms.api.rf_receiving.po_info', { po: res.name });
				st.po = info; st.lines = [];
				if (info.fully_received) toast('PO fully received — nothing pending', 'err');
				else toast(`${info.pending_count} item(s) pending`, 'ok');
				screenReceive();
			} catch (e) { toast(e.message, 'err'); input.select(); }
		}, '', { context: 'po' });
		body.push(scan);
	} else if (st.po.fully_received) {
		body.push(el('div', { class: 'card' },
			row(`PO ${st.po.po}`, st.po.supplier_name, el('button', { class: 'pill', onclick: () => { st.po = null; st.lines = []; screenReceive(); } }, 'Close')),
			el('div', { class: 'small' }, `Nothing pending — this PO is fully received (status: ${st.po.status || 'Completed'}). Scan a different PO.`),
		));
		body.push(el('button', { class: 'big secondary', onclick: () => { st.po = null; st.lines = []; screenReceive(); } }, 'Scan another PO'));
		render(topbar('Receiving'), ...body);
		return;
	} else {
		body.push(el('div', { class: 'card' },
			row(`PO ${st.po.po}`, st.po.supplier_name, el('button', { class: 'pill', onclick: () => { st.po = null; st.lines = []; screenReceive(); } }, 'Close')),
		));

		for (const item of st.po.items) {
			const line = st.lines.find((l) => l.item_code === item.item_code);
			const right = line
				? el('span', { class: 'pill ok' }, `${line.qty} / ${item.pending_qty}`)
				: el('span', { class: 'qty' }, `${item.pending_qty}`);
			body.push(el('div', {
				class: 'card', onclick: () => { st.activeItem = item; go('#receive-item'); },
				style: 'cursor:pointer',
			},
				row(item.item_name || item.item_code,
					`${item.item_code}${item.has_batch_no ? ' · batch' : ''}${item.has_serial_no ? ' · serial' : ''}`,
					right),
			));
		}

		body.push(el('button', {
			class: 'primary good',
			onclick: async () => {
				if (!st.lines.length) return toast('Scan at least one item first', 'err');
				try {
					const res = await api('fwms.api.rf_receiving.commit_receipt', {
						po: st.po.po,
						lpn_barcode: st.lpn || '',
						lines: JSON.stringify(st.lines),
					});
					toast(`Received — PR ${res.purchase_receipt}, LPN ${res.lpn}`, 'ok');
					st.po = null; st.lines = []; st.lpn = null;
					go('#receive');
				} catch (e) { toast(e.message, 'err'); }
			},
		}, `Commit receipt (${st.lines.reduce((a, l) => a + (l.qty || 0), 0)} units)`));
	}
	render(topbar('Receiving'), ...body);
	if (st.po) {
		const lpnScan = suggestCard('Scan tote/pallet LPN (optional)', 'LPN-… or CART-…', async (val, input) => {
			try {
				const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'receive-lpn' });
				if (res.type !== 'lpn') throw new Error(`Not an LPN (${res.type})`);
				st.lpn = res.name; toast(`Receiving into ${res.name}`, 'ok');
			} catch (e) { toast(e.message, 'err'); input.select(); }
		}, '', { context: 'lpn' });
		body.splice(1, 0, lpnScan);
		app.querySelector('.screen').replaceChildren(...body);
		lpnScan.focusScan();
	}
}

function screenReceiveItem() {
	const item = S.receive.activeItem;
	if (!item) return go('#receive');
	const line = S.receive.lines.find((l) => l.item_code === item.item_code) || { item_code: item.item_code, qty: 0, qc: 'Pass' };
	if (!S.receive.lines.includes(line)) S.receive.lines.push(line);

	const rerender = () => screenReceiveItem();

	const qty = el('input', { type: 'number', inputmode: 'decimal', class: 'qty-input', value: String(line.qty || item.pending_qty) });
	qty.addEventListener('change', () => {
		const n = parseFloat(qty.value) || 0;
		line.qty = n;
		if (item.has_serial_no) {
			line.serials = (line.serials || []).slice(0, Math.max(0, Math.round(n)));
			rerender();
		}
	});
	const batch = el('input', { type: 'text', class: 'qty-input mono', placeholder: item.has_batch_no ? 'Scan batch (blank = auto)' : '—', value: line.batch_no || '', disabled: item.has_batch_no ? null : 'disabled' });
	batch.addEventListener('input', () => { line.batch_no = batch.value.trim() || null; });

	// serials: one row per unit; scan fills the next row; Auto generates the
	// next sequential block (SN-{ITEM}-00001 style) from the server
	let serialScan = null, serialTable = null;
	if (item.has_serial_no) {
		const need = Math.max(0, Math.round(line.qty || 0));
		line.serials = line.serials || [];

		const rowsEl = el('div', { class: 'serial-rows' });
		const renderRows = () => {
			rowsEl.replaceChildren(...Array.from({ length: Math.max(need, line.serials.length) }, (_, i) => {
				const s = line.serials[i];
				return el('div', { class: `rowline ${s ? '' : 'pending'}`, style: 'padding:5px 0' },
					el('span', { class: 'small mono' }, `#${i + 1}`),
					el('span', { class: 'mono', style: 'flex:1;margin-left:8px' }, s || '— scan —'),
					s ? el('span', { class: 'pill ok' }, '✓') : null,
				);
			}));
		};
		renderRows();
		serialTable = el('div', { class: 'card' },
			el('div', { class: 'section-title' }, `Serials — one per unit (${line.serials.length}/${need})`),
			rowsEl,
			el('button', {
				class: 'big secondary', onclick: async (e) => {
					e.target.disabled = true;
					try {
						if (!(line.qty > 0)) throw new Error('Set the qty first');
						const res = await api('fwms.api.rf_receiving.next_serials', {
							item_code: item.item_code, qty: Math.round(line.qty),
						});
						line.serials = res.serials.slice(0, Math.round(line.qty));
						toast(`Generated ${line.serials.length} sequential serials`, 'ok');
						rerender();
					} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
				},
			}, '⚡ Auto-generate sequential'),
		);

		serialScan = scanCard(`Scan serials (${line.serials.length}/${need})`, 'SN-…', async (val, input) => {
			if (line.serials.includes(val)) { toast('Serial already scanned', 'err'); return input.select(); }
			if (line.serials.length >= need) { toast(`All ${need} serials scanned`, 'ok'); return input.select(); }
			line.serials.push(val);
			toast(`Serial ${line.serials.length}/${need}`, 'ok');
			rerender();
		});
	}

	render(topbar(`Receiving · ${item.item_code}`),
		el('div', { class: 'card' },
			row(item.item_name || item.item_code, item.item_code, el('span', { class: 'qty' }, `max ${item.pending_qty}`)),
			qty, qtyButtons(qty),
		),
		batch && item.has_batch_no ? el('div', { class: 'card' }, el('label', { class: 'small' }, 'Batch'), batch) : null,
		serialTable || '',
		serialScan || '',
		el('div', { class: 'card' },
			el('div', { class: 'section-title' }, 'QC decision'),
			el('div', { style: 'display:flex;gap:10px' },
				el('button', {
					class: 'primary good', style: 'flex:1',
					onclick: () => { line.qc = 'Pass'; toast('Marked Pass', 'ok'); go('#receive'); },
				}, '✔ Pass'),
				el('button', {
					class: 'primary danger', style: 'flex:1',
					onclick: () => { line.qc = 'Fail'; toast('Marked Fail → QC Hold', 'ok'); go('#receive'); },
				}, '✖ Fail'),
			),
			el('div', { class: 'small' }, 'Failed qty goes to QC Hold + Quality Inspection'),
		),
	);
}

// ------------------------------------------------ putaway

async function screenPutaway() {
	const tasks = await api('fwms.api.rf_putaway.open_tasks').catch(() => []);
	const body = [el('div', { class: 'section-title' }, 'Putaway queue')];
	if (!tasks.length) body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'Queue is empty 🎉')));
	for (const task of tasks) {
		body.push(el('div', { class: 'card', style: 'cursor:pointer', onclick: () => { S.putawayTask = task; go('#putaway-do'); } },
			row(`${task.lpn || 'LPN?'}`, `${task.qty || ''} units · ${task.status}`,
				el('span', { class: task.priority === '1-High' ? 'pill err' : 'pill' }, task.priority))));
	}
	render(topbar('Putaway'), ...body);
}

async function screenPutawayDo() {
	const task = S.putawayTask;
	if (!task) return go('#putaway');
	let suggestion = null;
	try { suggestion = await api('fwms.api.rf_putaway.suggest_bin', { lpn: task.lpn }); } catch (e) { /* no bin */ }
	const suggested = suggestion ? suggestion.bin : null;

	const scan = suggestCard('Scan LPN', task.lpn || 'LPN-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'putaway-lpn' });
			if (res.type !== 'lpn') throw new Error(`Not an LPN (${res.type})`);
			S.putawayLpn = res.name;
			toast('LPN confirmed — scan bin', 'ok');
			binScan.focusScan();
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'lpn' });

	const binScan = suggestCard(`Scan bin${suggested ? ` (suggested: ${suggested})` : ''}`, suggested || 'BULK-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'putaway-bin' });
			if (res.type !== 'bin') throw new Error(`Not a bin (${res.type})`);
			const lpn = S.putawayLpn || task.lpn;
			const out = await api('fwms.api.rf_putaway.confirm', { lpn, bin_name: res.name, task: task.name });
			toast(`Put away ${out.lpn} → ${out.bin}`, 'ok');
			go('#putaway');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'bin' });

	render(topbar('Putaway · directed'),
		el('div', { class: 'card' }, row(task.lpn, suggestion ? suggestion.reason : 'no suggestion yet')),
		scan, binScan,
	);
	scan.focusScan();
}

// ------------------------------------------------ pick

async function screenPick() {
	const body = [];
	if (!S.pick.task) {
		body.push(el('button', {
			class: 'primary', onclick: async (e) => {
				e.target.disabled = true;
				try {
					const res = await api('fwms.api.rf_picking.claim', { cart_barcode: S.pick.cart || '' });
					if (!res.task) { toast('No pick tasks in queue'); return; }
					S.pick.task = res.task; S.pick.cart = res.cart;
					toast(`Task ${res.task.name || res.task}`, 'ok');
					go('#pick-do');
				} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
			},
		}, 'Claim next pick task'));
		body.push(suggestCard('Scan cart LPN (optional)', 'CART-…', async (val, input) => {
			S.pick.cart = val; toast(`Cart ${val}`, 'ok');
		}, S.pick.cart || '', { context: 'cart' }));
	} else {
		go('#pick-do'); return;
	}
	render(topbar('Picking'), ...body);
}

async function screenPickDo() {
	const taskName = S.pick.task && S.pick.task.name ? S.pick.task.name : S.pick.task;
	let detail;
	try {
		detail = await api('fwms.api.rf_picking.task_detail', { task: taskName });
	} catch (e) { toast(e.message, 'err'); S.pick.task = null; return go('#pick'); }

	let stage = S.pick.stage || 'bin';
	const qty = el('input', { type: 'number', inputmode: 'decimal', class: 'qty-input', value: String(detail.qty) });
	const serials = detail.serials_expected || [];

	const binScan = suggestCard('1 · Scan bin', detail.from_bin || 'BIN-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'pick-bin' });
			if (res.type !== 'bin') throw new Error(`Not a bin (${res.type})`);
			if (detail.from_bin && res.name !== detail.from_bin) toast(`Wrong bin — task wants ${detail.from_bin}`, 'err');
			else { stage = 'item'; S.pick.stage = stage; toast('Bin ok — scan item', 'ok'); itemScan.focusScan(); }
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, detail.from_bin || '', { context: 'bin' });

	const itemScan = suggestCard('2 · Scan item', detail.item || 'ITEM…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'pick-item' });
			if (res.type !== 'item') throw new Error(`Not an item (${res.type})`);
			if (detail.item && res.name !== detail.item) throw new Error(`Wrong item — task wants ${detail.item}`);
			S.pick.stage = 'confirm'; toast('Item ok', 'ok');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, detail.item || '', { context: 'item' });

	const serialScan = serials.length ? suggestCard('3 · Scan serials', 'SN-…', async (val, input) => {
		S.pick.serials = S.pick.serials || [];
		if (!serials.includes(val)) return toast('Serial not expected for this task', 'err');
		if (S.pick.serials.includes(val)) return toast('Already scanned', 'err');
		S.pick.serials.push(val);
		toast(`Serial ${S.pick.serials.length}/${detail.qty}`, 'ok');
		input.select();
	}, '', { items: serials.map((s) => ({ value: s, label: s, desc: 'expected for this task' })) }) : null;

	const batchNote = detail.batch_no
		? el('div', { class: 'card' }, el('div', { class: 'small' }, `FEFO batch offered: `), el('b', { class: 'mono' }, detail.batch_no))
		: null;

	render(topbar(`Pick · ${detail.sales_order || ''}`),
		el('div', { class: 'card' },
			row(detail.item, `${detail.qty} units`, el('span', { class: 'pill ok' }, 'FEFO')),
			row(detail.from_bin || 'any pick face', detail.wave || ''),
			detail.batch_no ? row('Batch', detail.batch_no) : null,
		),
		batchNote || '',
		binScan, itemScan, serialScan || '',
		el('div', { class: 'card' },
			el('label', { class: 'small' }, 'Confirm qty'), qty, qtyButtons(qty),
			el('button', {
				class: 'primary good', style: 'margin-top:10px',
				onclick: async () => {
					try {
						const out = await api('fwms.api.rf_picking.pick_confirm', {
							task: taskName, qty: parseFloat(qty.value) || 0,
							cart: S.pick.cart || `CART-${S.worker.worker_code}`,
							scanned_serials: JSON.stringify(S.pick.serials || []),
						});
						toast(`Picked ${out.picked} — ${out.remaining} left`, 'ok');
						S.pick.task = null; S.pick.stage = null; S.pick.serials = [];
						go('#pick');
					} catch (e) { toast(e.message, 'err'); }
				},
			}, `Pick into ${S.pick.cart || 'cart'}`),
		),
	);
	binScan.focusScan();
}

// ------------------------------------------------ pack

async function screenPack() {
	const queue = await api('fwms.api.rf_packing.queue').catch(() => []);
	const body = [el('div', { class: 'section-title' }, `Pack queue (${queue.length})`)];
	for (const cart of queue) {
		body.push(el('div', { class: 'card', style: 'cursor:pointer', onclick: () => { S.pack.cart = cart.name; go('#pack-do'); } },
			row(cart.name, `${(cart.items || []).length} item(s) · ${cart.sales_order || ''}`,
				el('span', { class: 'pill warn' }, 'awaiting pack'))));
	}
	if (!queue.length) body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'Nothing to pack 🎉')));
	render(topbar('Packing'), ...body);
}

async function screenPackDo() {
	const cart = S.pack.cart;
	if (!cart) return go('#pack');
	let start;
	try { start = await api('fwms.api.rf_packing.start', { cart }); }
	catch (e) { toast(e.message, 'err'); return go('#pack'); }

	const mismatch = el('div', { class: 'card' });
	const cartMap = {};
	for (const r of start.cart_items || []) cartMap[`${r.item}|${r.batch_no || ''}`] = (cartMap[`${r.item}|${r.batch_no || ''}`] || 0) + r.qty;
	let ok = true;
	for (const r of start.dn_items || []) {
		const have = cartMap[`${r.item_code}|`];
		if (have === undefined) { ok = false; mismatch.append(row(r.item_code, 'on DN, not on cart', el('span', { class: 'pill err' }, 'missing'))); }
	}
	mismatch.prepend(row(`DN ${start.delivery_note}`, ok ? 'cart matches order' : 'check items below', ok ? el('span', { class: 'pill ok' }, 'ok') : el('span', { class: 'pill warn' }, 'review')));

	render(topbar(`Pack · ${cart}`),
		mismatch,
		el('div', { class: 'card' },
			el('button', {
				class: 'primary good', onclick: async (e) => {
					e.target.disabled = true;
					try {
						const caseNo = await api('fwms.api.rf_packing.next_case_no', { delivery_note: start.delivery_note });
						const out = await api('fwms.api.rf_packing.finish', { cart, delivery_note: start.delivery_note, case_no: caseNo });
						toast(`Packed — PS ${out.packing_slip}`, 'ok');
						const blob = new Blob([out.zpl], { type: 'application/octet-stream' });
						const a = el('a', { href: URL.createObjectURL(blob), download: `${cart}.zpl` });
						a.click();
						go('#pack');
					} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
				},
			}, 'Seal carton + print ZPL label'),
			el('div', { class: 'small' }, 'Creates Packing Slip vs the draft Delivery Note'),
		),
	);
}

// ------------------------------------------------ count

function screenCount() {
	const scan = suggestCard('Scan bin to count', 'BULK-A01-R02-B03', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'count' });
			if (res.type !== 'bin') throw new Error(`Not a bin (${res.type})`);
			const info = await api('fwms.api.rf_count.start', { bin_name: res.name });
			S.count = { ...info, counted: (info.expected || []).map((r) => ({ item: r.item, qty: r.qty, batch_no: r.batch_no })) };
			go('#count-do');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'bin' });
	render(topbar('Cycle counting'),
		el('div', { class: 'card' }, el('div', { class: 'small' }, 'Blind by default — expected qty hidden unless the Count Program allows it.')),
		scan,
	);
}

function screenCountDo() {
	const st = S.count;
	if (!st || !st.bin) return go('#count');
	const lines = st.counted && st.counted.length ? st.counted : [{ item: '', qty: 0, batch_no: null }];

	const itemScan = suggestCard('Scan item (or + to add line)', 'ITEM…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'count-item' });
			if (res.type !== 'item') throw new Error(`Not an item (${res.type})`);
			let line = st.counted.find((l) => l.item === res.name);
			if (!line) { line = { item: res.name, qty: 0, batch_no: null }; st.counted.push(line); }
			S.count.active = res.name;
			toast(`${res.name} — enter qty`, 'ok');
			screenCountDo();
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'item' });

	const cards = st.counted.map((line, idx) => {
		const qty = el('input', { type: 'number', inputmode: 'decimal', class: 'qty-input', value: String(line.qty || 0) });
		qty.addEventListener('input', () => { line.qty = parseFloat(qty.value) || 0; });
		return el('div', { class: 'card' },
			row(line.item || '(scan item)', line.batch_no ? `batch ${line.batch_no}` : '',
				el('button', { class: 'pill err', onclick: () => { st.counted.splice(idx, 1); screenCountDo(); } }, '✕')),
			qty, qtyButtons(qty));
	});

	render(topbar(`Count · ${st.bin}`),
		el('div', { class: 'card' }, row(st.bin, `${st.method} count${st.program ? ' · ' + st.program : ''}`,
			st.method === 'Expected' && st.expected ? el('span', { class: 'pill' }, 'expected shown') : el('span', { class: 'pill warn' }, 'blind'))),
		itemScan,
		...cards,
		st.counted.length ? el('button', {
			class: 'primary good', onclick: async () => {
				try {
					const out = await api('fwms.api.rf_count.submit', {
						bin_name: st.bin, counted: JSON.stringify(st.counted), task: st.task || '',
					});
					toast(`Variance ${out.variance} · accuracy ${out.accuracy}%${out.stock_reconciliation ? ' · SR ' + out.stock_reconciliation : ''}`, 'ok');
					S.count = {};
					go('#count');
				} catch (e) { toast(e.message, 'err'); }
			},
		}, 'Submit count') : '',
	);
	itemScan.focusScan();
}

// ------------------------------------------------ replenish

async function screenReplenish() {
	const tasks = await api('fwms.api.rf_replenish.open_tasks').catch(() => []);
	const body = [el('div', { class: 'section-title' }, 'Replenishment queue')];
	if (!tasks.length) body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'No pick-face shortages detected 🎉')));
	for (const task of tasks) {
		body.push(el('div', { class: 'card', style: 'cursor:pointer', onclick: () => { S.repl = task; go('#replenish-do'); } },
			row(`${task.qty} x ${task.item}`, `→ ${task.to_bin}`, el('span', { class: 'pill err' }, task.priority))));
	}
	render(topbar('Replenishment'), ...body);
}

async function screenReplenishDo() {
	const task = S.repl;
	if (!task) return go('#replenish');
	// staged flow state: { from, batch, avail, qty, to }
	if (!S.replFlow || S.replFlow.task !== task.name) {
		S.replFlow = { task: task.name, from: null, batch: null, avail: 0, qty: 0, to: null };
	}
	const F = S.replFlow;
	let sources = [];
	try { sources = await api('fwms.api.rf_replenish.source_bins', { item: task.item }); } catch (e) { /* none */ }

	const body = [el('div', { class: 'card' },
		row(`Task: ${task.qty} × ${task.item}`, `replenish ${task.to_bin}`,
			el('span', { class: task.priority === '1-High' ? 'pill err' : 'pill' }, task.priority)),
	)];

	// stage 1 — source bin
	if (!F.from) {
		const fromScan = suggestCard('1 · Scan bulk source bin', (sources[0] && sources[0].bin) || 'BULK-…', async (val, input) => {
			try {
				const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'repl-from' });
				if (res.type !== 'bin') throw new Error(`Not a bin (${res.type})`);
				const src = sources.find((s) => s.bin === res.name);
				if (!src) throw new Error(`No ${task.item} in ${res.name} — scan a bin from the list`);
				F.from = res.name; F.batch = src.batch_no || ''; F.avail = src.qty;
				F.qty = Math.min(task.qty, src.qty);
				toast(`${res.name}: ${src.qty} available`, 'ok');
				screenReplenishDo();
			} catch (e) { toast(e.message, 'err'); input.select(); }
		}, '', { context: 'bin', filter: (r) => sources.some((s) => s.bin === r.value) });
		if (sources.length) {
			body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'Bulk stock found:'),
				...sources.map((s) => row(s.bin, `batch ${s.batch_no || '—'}`, el('span', { class: 'qty' }, `${s.qty}`)))));
		} else {
			body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'No bulk stock of this item — wait for inbound or check FWMS LPN list.')));
		}
		body.push(fromScan);
		render(topbar(`Replenish · step 1 of 3`), ...body);
		fromScan.focusScan();
		return;
	}

	// stage 2 — qty (bounded by availability) + destination scan
	const src = sources.find((s) => s.bin === F.from) || { batch_no: F.batch, qty: F.avail };
	const qtyInput = el('input', { type: 'number', inputmode: 'decimal', class: 'qty-input', value: String(F.qty) });
	qtyInput.addEventListener('input', () => { F.qty = parseFloat(qtyInput.value) || 0; });

	const toScan = suggestCard(`2 · Scan destination pick face (task wants ${task.to_bin})`, task.to_bin, async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'repl-to' });
			if (res.type !== 'bin') throw new Error(`Not a bin (${res.type})`);
			if (res.name !== task.to_bin) throw new Error(`Wrong bin — task wants ${task.to_bin}`);
			F.to = res.name;
			toast('Destination ok — confirm transfer', 'ok');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { items: [{ value: task.to_bin, label: task.to_bin, desc: 'task target' }] });

	body.push(
		el('div', { class: 'card' },
			row(`From ${F.from}`, `batch ${src.batch_no || '—'}`, el('span', { class: 'qty' }, `${src.qty} available`)),
			el('label', { class: 'small' }, `Move qty (max ${Math.min(task.qty, src.qty)})`), qtyInput,
			qtyButtons(qtyInput),
			el('button', {
				class: 'pill', style: 'font-size:0.9rem;cursor:pointer',
				onclick: () => { F.from = null; F.batch = null; F.avail = 0; F.qty = 0; F.to = null; screenReplenishDo(); },
			}, '↺ change source'),
		),
		toScan,
		el('button', {
			class: 'primary good', onclick: async (e) => {
				e.target.disabled = true;
				try {
					if (!F.to) throw new Error(`Scan destination ${task.to_bin} first`);
					if (!(F.qty > 0)) throw new Error('Move qty must be positive');
					if (F.qty > src.qty) throw new Error(`Only ${src.qty} in ${F.from}`);
					const out = await api('fwms.api.rf_replenish.confirm', {
						task: task.name, from_bin: F.from, qty: F.qty, batch_no: F.batch || '',
					});
					toast(`Moved ${F.qty} × ${task.item} → ${F.to} (${out.stock_entry})`, 'ok');
					S.repl = null; S.replFlow = null;
					go('#replenish');
				} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
			},
		}, `Confirm · move ${F.qty} → ${task.to_bin}`),
	);
	render(topbar('Replenishment · step 2 of 3'), ...body);
	toScan.focusScan();
}

// ------------------------------------------------ load & ship

function screenLoadShip() {
	const dockScan = suggestCard('Scan dock door', 'OUT-01', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'load-dock' });
			if (res.type !== 'dock') throw new Error(`Not a dock door (${res.type})`);
			S.load.dock = res.name;
			toast(`Dock ${res.name}`, 'ok');
			go('#load-ship-do');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'dock', filter: (r) => r.desc.startsWith('Outbound') });
	render(topbar('Load & Ship'), dockScan);
}

async function screenLoadShipDo() {
	const dock = S.load.dock;
	if (!dock) return go('#loadship');
	let carts = [];
	try { carts = await api('fwms.api.rf_load.loaded_cartoons', { dock_door: dock }); } catch (e) { /* none */ }
	const staged = S.load.staged || [];

	const cartScan = suggestCard('Scan packed carton', 'CART-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'load-carton' });
			if (res.type !== 'lpn') throw new Error(`Not a carton (${res.type})`);
			const trailer = prompt('Trailer / container no (optional)') || '';
			const out = await api('fwms.api.rf_load.load', { cart: res.name, dock_door: dock, trailer });
			staged.push(res.name);
			toast(`Loaded ${res.name}`, 'ok');
			input.select();
			screenLoadShipDo();
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'packed_cart' });

	render(topbar(`Loading · ${dock}`),
		el('div', { class: 'section-title' }, `Staged at Shipping Docks (${carts.length})`),
		carts.length ? el('div', { class: 'card' }, ...carts.map((c) => row(c.name, c.delivery_note || ''))) :
			el('div', { class: 'card' }, el('div', { class: 'small' }, 'Scan packed cartons to load')),
		cartScan,
		el('button', {
			class: 'primary good', onclick: async (e) => {
				e.target.disabled = true;
				try {
					const out = await api('fwms.api.rf_load.ship', { dock_door: dock });
					toast(`Shipped ${out.shipped.length} DN(s) 🚛`, 'ok');
					S.load = { dock: null };
					go('#loadship');
				} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
			},
		}, 'Close trailer & ship'),
	);
	cartScan.focusScan();
}

// ------------------------------------------------ returns

function screenReturns() {
	const scan = suggestCard('Scan original Delivery Note', 'DN-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'returns' });
			if (res.type !== 'dn') throw new Error(`Not a Delivery Note (${res.type})`);
			const info = await api('fwms.api.rf_returns.dn_info', { delivery_note: res.name });
			S.returns = { dn: info, lines: [] };
			go('#returns-do');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'dn' });
	render(topbar('Returns'), scan);
}

function screenReturnsDo() {
	const st = S.returns;
	if (!st || !st.dn) return go('#returns');

	const itemScan = suggestCard('Scan returned item', 'ITEM…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'returns-item' });
			if (res.type !== 'item') throw new Error(`Not an item (${res.type})`);
			const item = st.dn.items.find((i) => i.item_code === res.name);
			if (!item) throw new Error(`${res.name} is not on ${st.dn.delivery_note}`);
			let line = st.lines.find((l) => l.item_code === res.name);
			if (!line) { line = { item_code: res.name, qty: 0, grade: 'A' }; st.lines.push(line); }
			toast('Enter qty + grade', 'ok');
			screenReturnsDo();
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'item' });

	const cards = st.lines.map((line, idx) => {
		const qty = el('input', { type: 'number', class: 'qty-input', value: String(line.qty || 0) });
		qty.addEventListener('input', () => { line.qty = parseFloat(qty.value) || 0; });
		const grade = el('select', { class: 'grade' },
			...['A', 'B', 'Refurb', 'Scrap'].map((g) => el('option', { value: g, selected: line.grade === g ? '' : null }, g)));
		grade.addEventListener('change', () => { line.grade = grade.value; });
		return el('div', { class: 'card' },
			row(line.item_code, `max ${st.dn.items.find((i) => i.item_code === line.item_code)?.returnable_qty || '…'}`),
			qty, qtyButtons(qty), grade);
	});

	render(topbar(`Returns · ${st.dn.delivery_note}`),
		el('div', { class: 'card' }, row(st.dn.delivery_note, st.dn.customer)),
		itemScan, ...cards,
		st.lines.length ? el('button', {
			class: 'primary good', onclick: async (e) => {
				e.target.disabled = true;
				try {
					const out = await api('fwms.api.rf_returns.process_return', {
						delivery_note: st.dn.delivery_note, lines: JSON.stringify(st.lines),
					});
					toast(`Return drafted: ${(out.returns || []).map((r) => r.return).join(', ')}`, 'ok');
					S.returns = {};
					go('#returns');
				} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
			},
		}, 'Process return') : '',
	);
	itemScan.focusScan();
}

// ------------------------------------------------ quality (QC Hold disposition)

async function screenQuality() {
	const tasks = await api('fwms.api.rf_quality.open_tasks').catch(() => []);
	const body = [el('div', { class: 'section-title' }, 'Quality queue — QC Hold')];
	if (!tasks.length) body.push(el('div', { class: 'card' }, el('div', { class: 'small' }, 'No QC-hold inspections pending 🎉')));
	for (const task of tasks) {
		body.push(el('div', { class: 'card', style: 'cursor:pointer', onclick: () => { S.quality.task = task; go('#quality-do'); } },
			row(`${task.lpn || 'LPN?'}`, `${task.qty || ''} units · ${task.status}`,
				el('span', { class: task.priority === '1-High' ? 'pill err' : 'pill' }, task.priority))));
	}
	render(topbar('Quality'), ...body);
}

async function screenQualityDo() {
	const task = S.quality.task;
	if (!task) return go('#quality');
	let detail;
	try {
		detail = await api('fwms.api.rf_quality.task_detail', { task: task.name });
	} catch (e) { toast(e.message, 'err'); return go('#quality'); }

	const st = S.quality;
	st.results = detail.items.map((it) => ({
		item_code: it.item_code, qty: it.qty, batch_no: it.batch_no || '', verdict: 'Pass',
	}));

	const rerender = () => {
		const cards = st.results.map((line) => {
			const qty = el('input', { type: 'number', value: String(line.qty), min: '0', step: '1' });
			qty.addEventListener('input', () => { line.qty = parseFloat(qty.value) || 0; });
			return el('div', { class: 'card' },
				row(line.item_code, line.batch_no ? `batch ${line.batch_no}` : '',
					el('span', { class: `pill ${line.verdict === 'Pass' ? 'ok' : 'err'}` }, line.verdict)),
				qty, qtyButtons(qty),
				el('div', { style: 'display:flex;gap:10px' },
					el('button', {
						class: `pill ${line.verdict === 'Pass' ? 'ok' : ''}`, style: 'flex:1;cursor:pointer',
						onclick: () => { line.verdict = 'Pass'; rerender(); },
					}, '✔ Pass'),
					el('button', {
						class: `pill ${line.verdict === 'Fail' ? 'err' : ''}`, style: 'flex:1;cursor:pointer',
						onclick: () => { line.verdict = 'Fail'; rerender(); },
					}, '✖ Fail'),
				),
			);
		});
		render(topbar(`Quality · ${detail.lpn}`),
			el('div', { class: 'card' }, row(detail.lpn,
				`${detail.source_doctype || ''} ${detail.source_name || ''} · ${detail.zone || ''}`)),
			...cards,
			el('button', {
				class: 'primary good', onclick: async (e) => {
					e.target.disabled = true;
					try {
						const out = await api('fwms.api.rf_quality.inspect', {
							lpn: detail.lpn, task: task.name,
							results: JSON.stringify(st.results),
						});
						toast(out.message, 'ok');
						if ((out.held || []).length) {
							S.scrap.held = { lpn: detail.lpn, lines: out.held };
							go('#quality-hold');
						} else {
							S.quality = { task: null, results: [] };
							go('#quality');
						}
					} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
				},
			}, 'Submit inspection'),
		);
	};
	rerender();
}

function screenQualityHold() {
	const held = S.scrap.held;
	if (!held) return go('#quality');
	S.quality = { task: null, results: [] };
	S.scrap.held = null;
	S.scrap.lpn = held.lpn;
	S.scrap.info = null;
	S.scrap.lines = held.lines.map((l) => ({ item_code: l.item_code, qty: l.qty, batch_no: l.batch_no || '' }));
	const units = held.lines.reduce((a, l) => a + l.qty, 0);
	render(topbar(`QC Hold · ${held.lpn}`),
		el('div', { class: 'card' }, el('div', { class: 'small' },
			`${units} unit(s) failed inspection and are held on QC Hold in ${held.lpn}.`)),
		el('button', {
			class: 'primary danger', style: 'margin-top:8px',
			onclick: () => go('#scrap-do'),
		}, '🗑 Scrap failed units now'),
		el('button', {
			class: 'big secondary', style: 'margin-top:8px',
			onclick: () => go('#quality'),
		}, 'Keep on QC Hold — desk decides'),
	);
}

// ------------------------------------------------ scrap (controlled write-off)

function screenScrap() {
	const scan = suggestCard('Scan LPN to scrap', 'LPN-…', async (val, input) => {
		try {
			const res = await api('fwms.api.rf_common.scan', { barcode: val, screen: 'scrap' });
			if (res.type !== 'lpn') throw new Error(`Not an LPN (${res.type})`);
			const info = await api('fwms.api.rf_scrap.lpn_info', { lpn: res.name });
			S.scrap = {
				lpn: info.lpn, info,
				lines: info.items.map((it) => ({ item_code: it.item_code, qty: it.qty, batch_no: it.batch_no || '' })),
				reason: S.scrap.reason || 'Damaged', held: null,
			};
			go('#scrap-do');
		} catch (e) { toast(e.message, 'err'); input.select(); }
	}, '', { context: 'lpn' });
	render(topbar('Scrap'),
		scan,
		el('div', { class: 'card' }, el('div', { class: 'small' },
			'Pick lines + reason, then file the draft. Inventory Control approves the Stock Entry before stock posts.')),
	);
	scan.focusScan();
}

async function screenScrapDo() {
	const st = S.scrap;
	if (!st.lpn) return go('#scrap');
	if (!st.info) st.info = await api('fwms.api.rf_scrap.lpn_info', { lpn: st.lpn }).catch(() => null);
	const REASONS = ['Damaged', 'Expired', 'Contamination', 'Obsolete', 'Quality Fail', 'Other'];

	const rerender = () => {
		const reason = el('select', { style: 'width:100%;padding:10px;font-size:1rem' },
			...REASONS.map((r) => el('option', { value: r }, r)));
		reason.value = st.reason;
		reason.addEventListener('change', () => { st.reason = reason.value; });

		const cards = st.lines.map((line, i) => {
			const qty = el('input', { type: 'number', value: String(line.qty), min: '0', step: '1' });
			qty.addEventListener('input', () => { line.qty = parseFloat(qty.value) || 0; });
			return el('div', { class: 'card' },
				row(line.item_code, line.batch_no ? `batch ${line.batch_no}` : '',
					el('button', {
						class: 'pill err', style: 'cursor:pointer',
						onclick: () => { st.lines.splice(i, 1); rerender(); },
					}, '✕')),
				qty, qtyButtons(qty),
			);
		});
		render(topbar(`Scrap · ${st.lpn}`),
			el('div', { class: 'card' }, row(st.lpn,
				st.info ? `${st.info.zone || ''}${st.info.bin ? ' · ' + st.info.bin : ''}` : '')),
			...cards,
			el('div', { class: 'card' }, el('label', { class: 'small' }, 'Reason'), reason),
			el('button', {
				class: 'primary danger', onclick: async (e) => {
					e.target.disabled = true;
					try {
						const out = await api('fwms.api.rf_scrap.scrap', {
							lpn: st.lpn, reason: st.reason || 'Damaged',
							lines: JSON.stringify(st.lines.filter((l) => l.qty > 0)),
						});
						toast(out.message, 'ok');
						S.scrap = { lpn: null, info: null, lines: [], reason: out.reason, held: null };
						go('#menu');
					} catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
				},
			}, 'File scrap draft'),
		);
	};
	rerender();
}

// ---------------------------------------------------------------- router

const ROUTES = {
	'#login': screenLogin,
	'#menu': async () => {
		if (!S.worker) return screenLogin();
		await screenMenu();
	},
	'#receive': async () => { if (!S.worker) return screenLogin(); await screenReceive(); },
	'#receive-item': async () => { if (!S.worker) return screenLogin(); screenReceiveItem(); },
	'#putaway': async () => { if (!S.worker) return screenLogin(); await screenPutaway(); },
	'#putaway-do': async () => { if (!S.worker) return screenLogin(); await screenPutawayDo(); },
	'#pick': async () => { if (!S.worker) return screenLogin(); await screenPick(); },
	'#pick-do': async () => { if (!S.worker) return screenLogin(); await screenPickDo(); },
	'#pack': async () => { if (!S.worker) return screenLogin(); await screenPack(); },
	'#pack-do': async () => { if (!S.worker) return screenLogin(); await screenPackDo(); },
	'#count': async () => { if (!S.worker) return screenLogin(); screenCount(); },
	'#count-do': async () => { if (!S.worker) return screenLogin(); screenCountDo(); },
	'#replenish': async () => { if (!S.worker) return screenLogin(); await screenReplenish(); },
	'#replenish-do': async () => { if (!S.worker) return screenLogin(); await screenReplenishDo(); },
	'#loadship': async () => { if (!S.worker) return screenLogin(); screenLoadShip(); },
	'#load-ship-do': async () => { if (!S.worker) return screenLogin(); await screenLoadShipDo(); },
	'#returns': async () => { if (!S.worker) return screenLogin(); screenReturns(); },
	'#returns-do': async () => { if (!S.worker) return screenLogin(); screenReturnsDo(); },
	'#quality': async () => { if (!S.worker) return screenLogin(); await screenQuality(); },
	'#quality-do': async () => { if (!S.worker) return screenLogin(); await screenQualityDo(); },
	'#quality-hold': async () => { if (!S.worker) return screenLogin(); screenQualityHold(); },
	'#scrap': async () => { if (!S.worker) return screenLogin(); screenScrap(); },
	'#scrap-do': async () => { if (!S.worker) return screenLogin(); await screenScrapDo(); },
};

window.addEventListener('hashchange', () => {
	const fn = ROUTES[location.hash] || ROUTES['#menu'];
	Promise.resolve(fn()).catch((e) => toast(e.message, 'err'));
});

// boot
(async () => {
	if (!location.hash) location.hash = S.worker ? '#menu' : '#login';
	else if (S.worker && location.hash === '#login') location.hash = '#menu';
	const fn = ROUTES[location.hash] || ROUTES['#menu'];
	try { await fn(); } catch (e) { toast(e.message, 'err'); }
})();
