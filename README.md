# FWMS — Warehouse Management on Frappe / ERPNext v16

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

FWMS turns warehouse floor operations into real ERPNext documents. RF-scan-driven
flows (receiving, putaway, picking, packing, loading, counting, replenishment,
returns) run against native ERP doctypes — Purchase Receipt, Stock Entry, Pick
List, Packing Slip, Delivery Note, Stock Reconciliation, Quality Inspection —
while a live floor-view dashboard visualises the warehouse in realtime.

Built and tested on: **Frappe 16.x + ERPNext 16.x, Python 3.14, MariaDB 11.8.**

## Layout

```
apps/fwms/fwms/
├── fwms/doctype/            # FWMS masters & log doctypes
│   ├── fwms_settings/       # feature flags, release mode, ZPL printer
│   ├── fwms_zone/           # zone tree + floor-view map coordinates
│   ├── fwms_bin/            # physical bins (FWMS-internal; ERP stock at zone level)
│   ├── fwms_count_program/  # ABC-class cycle-count scheduling
│   ├── fwms_worker/         # worker + PIN (RF login) + live state
│   ├── fwms_shift/  fwms_dock_door/
│   ├── fwms_lpn/            # license plates: container identity + contents
│   ├── fwms_event_log/      # the floor's event stream (realtime)
│   ├── fwms_task/           # self-assignment work queue (RF + future automation)
│   ├── fwms_wave/           # Continuous / Waves order release
│   ├── fwms_kpi_snapshot/   # 5-minute KPI rollups
│   └── fwms_consignment/    # export documentation driver
├── api/                     # whitelisted RF + dashboard endpoints
│   ├── rf_auth.py           # worker PIN login
│   ├── rf_receiving.py rf_putaway.py rf_picking.py rf_packing.py
│   ├── rf_count.py rf_replenish.py rf_load.py rf_returns.py
│   ├── floorview.py         # dashboard payload + simulation tick
│   └── pwa.py               # service worker / manifest under the /rf scope
├── wave.py                  # SO -> wave -> Pick List (FEFO/FIFO directed)
├── tasks.py                 # claim / start / complete task engine
├── events.py                # emit() + doc_events hooks -> realtime
├── scheduler_jobs.py        # counts, replenishment, KPI snapshots
├── gateways/                # Carrier / Customs / Automation interface stubs
├── printing/zpl.py          # ZPL label bridge (download or raw TCP 9100)
├── setup/                   # install bootstrap + demo seeder
├── tests/                   # unit + e2e (bench run-tests --app fwms)
├── www/rf.html              # RF terminal PWA shell  ->  /rf
├── www/floorview.html       # live floor dashboard   ->  /floorview
└── public/{rf,floorview}/   # vanilla-JS/CSS assets (no build step)
fwms/print_format/           # Commercial Invoice (Export), Certificate of
                            # Origin, Export Packing List, FWMS Packing Slip
```

## Architecture in one paragraph

ERP stock is tracked at **zone level** (Receiving Docks, QC Hold, Bulk, Pick
Faces, Scrap warehouses); physical bin truth lives inside FWMS as **LPN
contents per bin**. Crossing zones posts a native Stock Entry; moving within a
zone is an FWMS-only LPN move. Sales Orders release into **waves** (Continuous
or cutoff/route/dock batches) which build ERPNext Pick Lists whose rows are
ordered **FEFO/FIFO from real pick-face LPN stock**, with one directed pick
task per bin visit; waves sourced entirely from bulk complete themselves.
Workers claim tasks themselves; every RF scan lands in `FWMS Event Log` and is
pushed to the floor view over websocket.

## Setup

```bash
bench get-app erpnext --branch version-16   # if not present
bench get-app <this repo>                   # or clone into apps/fwms
bench --site yoursite install-app erpnext   # if not installed
bench --site yoursite install-app fwms
bench --site yoursite migrate               # doctypes, custom fields, print formats, roles, ERP permissions
bench --site yoursite execute fwms.setup.install.setup_company   # company + zones + warehouses + count programs
bench --site yoursite execute fwms.setup.seed_demo.run           # 26 workers, items, stock, POs, released SOs
bench build --app fwms
bench start
```

Then open (dev bench, default site):

| URL | What |
|---|---|
| `http://<site>:8000/rf` | RF terminal — PIN login `W001`…`W026`, PIN `0001`…`0026` |
| `http://<site>:8000/floorview` | Live floor view — KPIs, zone map, docks, workers, event stream, 11-step tour |
| `/app` Desk | FWMS doctypes, waves, consignments (`supervisor@fwms.local` seeded) |

Roles: **FWMS Operator** (RF flows), **FWMS Supervisor** (everything FWMS),
**FWMS Inventory Control** (count approval / Stock Reconciliation submit).

## RF flows (scan-first; keyboard-wedge scanners work unchanged)

1. **Receiving** — scan PO → item → qty (+ batch / serials) → QC Pass/Fail →
   Purchase Receipt into an LPN; failures route to QC Hold + Quality Inspection.
2. **Putaway** — task queue → suggested bin (pick face below max, else bulk) →
   scan LPN → scan bin → Stock Entry when the zone changes.
3. **Picking** — claim task → scan cart → bin → item; system offers the FEFO
   batch and validates serials → Pick List updates → submits on completion →
   draft Delivery Note(s).
4. **Packing** — scan cart → validate vs DN → Packing Slip + carton ZPL label.
5. **Load / Ship** — scan cartons → dock/trailer → close trailer → Delivery
   Note submits, wave closes, LPNs ship.
6. **Cycle counting** — scan bin → blind count (or expected per Count Program)
   → variance → Stock Reconciliation draft for Inventory Control approval.
7. **Replenishment** — pick-face min/max breaches → bulk → pick face transfer.
8. **Returns** — scan DN → item → grade A/B/Refurb/Scrap → Sales Return drafts
   routed to the right zone.

## Export documentation

`FWMS Consignment` groups Delivery Notes with transport mode, carrier,
incoterms and weights. Print formats included:

- **Commercial Invoice (Export)** — HS code + country of origin per line (from
  the native Item fields `customs_tariff_number` / `country_of_origin`)
- **Certificate of Origin** — chamber-style declaration
- **Export Packing List (Carton)** — carton-level contents from LPNs
- **FWMS Packing Slip** — for the native Packing Slip doctype

CMR, House B/L and House AWB formats arrive with the Phase 6 Carrier Gateway;
per-country export e-filing (AES / CDS / ATLAS / ICEGATE) behind the Phase 7
Customs Gateway. Both interfaces are declared in `fwms/gateways/`.

## Tests

```bash
bench --site yoursite set-config allow_tests true
bench --site yoursite run-tests --app fwms
```

Covers: receiving (batch/serial/QC), serial genealogy, putaway ledger moves,
wave FEFO ordering, wave closure after ship, blind counting + SR generation,
count scheduling, task engine skills/claims, and the full receive → putaway →
wave → pick → pack → load → ship cycle asserting the stock ledger, DN status,
LPN lifecycle and KPI snapshot. Verified green on both a long-lived dev site
and a freshly installed site.

## Dev harness

`fwms/dev.py` — isolated smoke checks via
`bench --site <site> execute fwms.dev.<fn>`: `check_receive`, `check_putaway`,
`check_pick`, `check_pack`, `check_ship`, `check_count`, `check_replenish`,
`check_kpis`, `check_floorview`, `check_consignment`, `check_pin_login`,
`seed_with_traceback` (seeder with real tracebacks — `bench execute` masks
function errors behind its eval fallback).

## Roadmap (interfaces already in place)

- **Phase 6** Carrier Gateway (`get_rates / create_label / track /
  schedule_pickup`) + CMR/House B/L/House AWB formats + cartonization + labor
  standards + task interleaving.
- **Phase 7** Customs Gateway export declarations per territory.
- **Phase 8** Automation Bridge (AS/RS, conveyor, AGV over the
  `execution_channel = Automated` task queue) + yard appointments + slotting
  advisor.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The app uses `pre-commit`
(ruff, ruff-format, eslint, prettier) and ships an integration test suite —
`bench --site yoursite run-tests --app fwms` — that runs against a seeded site.

## License

Copyright (c) 2026 Uma Anivilla. Released under the [GNU GPL v3](license.txt),
the same license as ERPNext — FWMS links against and extends ERPNext, and
stays copyleft with it.
