# FWMS — Manual Test Script

## Starting the system after a computer restart

MariaDB and Redis auto-start with WSL (systemd). Only the bench needs starting:

```bash
bash ~/frappe-bench/start-fwms.sh     # safe to run any time; idempotent
```

…or simply `cd ~/frappe-bench && bench start` (keep the terminal open). Wait
for the URL banner, then reload the browser. To auto-start at Windows logon,
create a Task Scheduler task (run once, as your user, at logon):

```
wsl.exe -d Ubuntu -u uma -- bash -lc "cd /home/uma/frappe-bench && nohup bash start-fwms.sh >/tmp/fwms-start.log 2>&1 &"
```

Walkthrough for a human acceptance pass of the FWMS system on the dev bench.
Every step lists **exactly what to do** and **what you should see**. Typing a
value and pressing **Enter** in any RF scan field is equivalent to scanning a
barcode — keyboard-wedge scanners work unchanged.

---

## 0. Pre-flight

| Check | Command / URL | Expected |
|---|---|---|
| Bench running | `bench start` (or check `http://localhost:8000`) | Page or 403 responds, no connection error |
| RF terminal | [http://localhost:8000/rf](http://localhost:8000/rf) | Dark login screen "FWMS / Warehouse RF Terminal" |
| Floor view | [http://localhost:8000/floorview](http://localhost:8000/floorview) | KPI cards, zone map, dock strip, event stream |
| Desk | [http://localhost:8000/app](http://localhost:8000/app) | Login page |

**Credentials**

| Who | Login | Where |
|---|---|---|
| Operator (26) | Worker `W001`…`W026`, PIN `0001`…`0026` | `/rf` |
| Supervisor | `supervisor@fwms.local` / `Supervisor#2026` | Desk + `/floorview` |
| Administrator | `Administrator` / `admin` | Desk |

**Seeded reference data** (names you will use while testing):
Zones `Receiving Docks, QC Hold, Bulk Storage, Pick Faces, Packing, Shipping Docks` ·
staging bins `RCV-STG-1…4` and `QC-STG-1…4` · bulk bins `BULK-A01…A05` (×rack ×bin) ·
pick faces `PICK-01…24` · docks `IN-01…3`, `OUT-01…3` · items `COCOA-100, PROTBAR-20,
JUICE-1L` (batch+expiry), `LAPTOP-14, PHONE-X, BATT-20` (serial), `STEEL-SHELF,
VALVE-08` (plain). The seeder also created 4 Purchase Orders and 4 released Sales
Orders — your floor may already have open tasks if they weren't consumed.

> Tip: if a queue you need is empty, each section says how to create its input.

> **After any fwms app update:** the RF terminal is a PWA — do one **hard
> refresh** (Ctrl+Shift+R, or DevTools → Application → Clear site data) the
> first time you open `/rf` after an update, so the browser drops the cached
> JS. Asset URLs carry a version query now, so this is only needed when
> moving from a pre-versioning checkout.

---

## 1. RF PIN login

1. Open `/rf`.
2. Worker code `W001`, PIN `0001`, **Sign in**.

**Expect:** green toast "Signed in: Alice Andersson", menu with 8 tiles
(Receiving, Putaway, Picking, Packing, Count, Replenish, Load/Ship, Returns).

3. Wrong PIN check: Logout, try `W001` / `9999`.

**Expect:** red toast "Invalid worker code or PIN".

---

## 2. Receiving — happy path (QC Pass)

*Input: an open Purchase Order.* If none: Desk → Buying → Purchase Order → New
(supplier **Cocoa West GmbH**, item `COCOA-100` qty 20, rate auto, Submit).
Note the PO number (e.g. `PUR-ORD-2026-00009`).

1. `/rf` → **Receiving** → scan/type the PO number → Enter.
   **Expect:** PO card with supplier; list of pending items.
2. (Optional) scan an LPN barcode in the *Scan tote/pallet LPN* box, e.g.
   `LPN-TEST-01` — it will be created. Otherwise the system mints one.
3. Tap the item row (`COCOA-100`).
4. Qty: enter `10`. Batch: leave blank (auto) or type `MANUAL-BATCH-01`.
5. **✔ Pass**.
6. Back on the PO screen → **Commit receipt (10 units)**.
   **Expect:** toast `Received — PR MAT-PRE-2026-00xx, LPN LPN-0000xx`.

**Desk verification (Supervisor):**
- Purchase Receipt list → the new PR is **Submitted**; row warehouse =
  `Receiving Docks - FW`; batch = the one used (auto batch id starts `RCV-…`).
- FWMS LPN list → the LPN with status **Receiving**, zone Receiving Docks,
  contents 10 × COCOA-100.
- FWMS Task list → an **Open Putaway** task for that LPN, priority 1-High.
- FWMS Event Log → a `Receiving & QC` entry "Received 10 units into …".

---

## 3. Receiving — QC Fail route

1. `/rf` → **Receiving** → scan another open PO (or the same one).
2. Pick item `VALVE-08`, qty `5`, **✖ Fail** → Commit.

**Expect / verify:**
- PR submitted with the row's **Rejected Warehouse = QC Hold - FW** (accepted qty 0).
- If `VALVE-08` has *inspection required before purchase* enabled, a **Quality
  Inspection** (status Rejected) is linked to the PR. (Item master → set
  "Inspection Required Before Purchase" to test this branch.)
- LPN contents do **not** include the failed qty.
- Event log shows the receipt; Stock Settings action for rejected QI is `Warn`.

---

## 4. Putaway — directed move

1. `/rf` → **Putaway**.
   **Expect:** queue card for the LPN received in §2.
2. Tap it.
   **Expect:** "suggested bin" — a `PICK-xx` face slotted for COCOA-100 if its
   primary item matches and it is below max, else a `BULK-…` bin.
3. Scan the LPN barcode (from the task card) → scan the suggested bin barcode
   (or type it).

**Expect:** toast `Put away LPN-… → PICK-xx`.

**Desk verification:**
- FWMS LPN: status **In Bin**, bin set, zone Pick Faces (or Bulk).
- Stock Entry list: a **Material Transfer** (Receiving Docks → target zone)
  submitted for the batch qty.
- Putaway task now **Complete**; event log entry `Putaway & Storage`.

---

## 5. Order release — wave

*If the pick queue is empty, create a fresh order:* Desk → Selling → Sales Order
→ New (customer **Nordic Retail AB**; item `COCOA-100` qty 5, warehouse
`Pick Faces - FW`, rate auto) → **Submit**.

**Expect immediately (Continuous mode):**
- FWMS Wave created, status **Released**, linked Pick List with FEFO-ordered
  rows (nearest-expiry batch first).
- FWMS Task list: Open **Pick** task(s), `from_bin` = a pick face.
- Floor view event stream: "Wave FW-WAVE-… released".

*Waves mode (optional):* FWMS Settings → Release Mode **Waves**; new SOs are
held ("order held for wave release" event); create an FWMS Wave manually in
Desk (add order, set a past Cutoff Time) → **Release Wave** action. Switch back
to Continuous afterwards.

---

## 6. Picking — bin-directed, FEFO, cart

1. `/rf` (worker with pick skill or none, e.g. `W002`/`0002`) → **Picking** →
   optionally scan a cart barcode (e.g. `CART-MANUAL-1`), then **Claim next
   pick task**.
   **Expect:** task card: item, qty, batch (FEFO), from-bin, wave.
2. **1 · Scan bin** → type the from-bin (e.g. `PICK-13`) → Enter.
3. **2 · Scan item** → type `COCOA-100` (or scan its EAN from Item master).
   *Wrong item on purpose once* → **Expect:** red toast "Wrong item — task
   wants …".
4. For serialized items (`LAPTOP-14`, `PHONE-X`, `BATT-20`): **3 · Scan
   serials** appears — scan serials from the Serial No list; a serial not in
   the task is rejected.
5. Confirm qty (default = task qty) → **Pick into cart**.
   **Expect:** toast `Picked N — 0 left`; task Complete.

**When the wave's last pick task completes, expect:** toast + desk shows Pick
List **Submitted**, FWMS Wave status **Picking Complete**, and a **draft
Delivery Note** per Sales Order. FWMS LPN: cart has the picked contents
(status Picking).

---

## 7. Packing — slip + ZPL label

1. `/rf` → **Packing**.
   **Expect:** pack queue lists the cart from §6 with its items.
2. Tap the cart.
   **Expect:** the linked draft DN and an item-match card (green "ok" when the
   cart matches the order).
3. **Seal carton + print ZPL label**.
   **Expect:** toast `Packed — PS MAT-PAC-…`, a `<cart>.zpl` download
   (open in a text editor — starts `^XA`, contains the cart barcode), Packing
   Slip **Submitted** against the DN, cart LPN status **Packed** in zone
   Packing, event `Packing`.

*ZPL printing to a real printer (optional):* FWMS Settings → ZPL Printer Host
= printer IP, port 9100. The label endpoint then can push raw ZPL.

---

## 8. Load & Ship — trailer close

1. `/rf` (e.g. `W005`/`0005`) → **Load / Ship** → scan dock `OUT-01`.
2. Scan each packed carton (`CART-MANUAL-1`…). Enter a trailer ref when
   prompted (e.g. `TLR-88`).
   **Expect:** "Loaded …" toast; dock door status **Loading** (Floor view dock
   strip turns red).
3. **Close trailer & ship**.
   **Expect:** toast `Shipped N DN(s)`; dock returns **Idle**; cartons
   **Shipped**.

**Desk verification:**
- Delivery Note(s) **Submitted** (Stock Ledger entries consume from
  `Pick Faces - FW`).
- FWMS Wave status **Closed**, its order row **Shipped** with the DN linked.
- Floor view KPI "Orders shipped" increments; event `Sort Load & Ship —
  trailer closed`.

---

## 9. Cycle counting — blind + variance + approval

1. `/rf` (e.g. `W006`/`0006`) → **Count** → scan a stocked bin (find one in
   Desk: FWMS LPN list → copy a `bin` from Bulk Storage, e.g. `BULK-A01-R1-B2`).
2. **Expect:** method **Blind** pill — expected qty is NOT shown.
3. Scan the bin's items (scan item barcode or type `COCOA-100`) and enter a
   count **one unit lower** than reality (check FWMS LPN contents in Desk
   first), then **Submit count**.

**Expect:** toast `Variance -1 · accuracy …% · SR MAT-RECO-…`.

**Desk (Supervisor / Inventory Control):**
- Stock Reconciliation list → the draft SR with the counted qty; FWMS Bin shows
  `Last Counted On = today`, variance −1, accuracy < 100 %.
- **Submit the SR** (Inventory Control role) → Stock Ledger posts the
  adjustment; event `Inventory Control`.
- Counter check: the count task (if scheduled by the Count Program) is Complete.

*Expected-count mode:* FWMS Count Program → set method **Expected** → the RF
count screen then shows expected quantities.

---

## 10. Replenishment — bulk → pick face

1. Desk: FWMS Bin → a pick face (e.g. `PICK-13`) → note `Primary Item`,
   `Min Qty`, `Max Qty`. Set Min `50` if current stock is below it (FWMS LPN
   aggregation in that bin < 50).
2. Run the scheduler once: `bench --site fwms.localhost execute
   fwms.dev.check_replenish` (or wait for the hourly job).
3. `/rf` → **Replenish** → **Expect:** task `N × <item> → PICK-13`, priority 1-High.
4. Tap it → the screen lists bulk stock (bin + batch) → scan/accept the bulk
   bin → **Confirm transfer**.

**Expect:** toast with Stock Entry id; FWMS LPN `RPL-PICK-13` refilled; Stock
Entry Material Transfer (Bulk → Pick Faces) submitted.

---

## 11. Returns — graded disposition

1. `/rf` → **Returns** → scan a **submitted** Delivery Note number (from §8).
2. Scan item (e.g. `COCOA-100`), qty `2`, grade **B** → **Process return**.

**Expect:** toast `Return drafted: MAT-DN-2026-000xx (Return)`; a **draft
Sales Return** (is_return) with warehouse `QC Hold - FW` for grade B; FWMS LPN
`RET-…` in QC Hold carrying the qty; event `Returns · grade B`.

Grade matrix: **A** → Pick Faces, **B** → QC Hold, **Refurb** → Bulk,
**Scrap** → Scrap warehouse. Submit the return in Desk to post stock.

---

## 12. Floor view — live behaviour

1. Open `/floorview`. **Expect:** redirect to the Desk login if signed out;
   after login (e.g. `supervisor@fwms.local`) the full canvas floor renders —
   warehouse walls and doors, racks A–I filled from **real bin stock** (fill =
   LPN qty, yellow tick = A-class, green outline = pick faces), staging
   pallets from actual receiving LPNs, 26 colour-coded workers, conveyor,
   carrier lanes, pack benches, KPI grid, event log.
2. Leave it open next to `/rf` and perform any RF action (e.g. §2 commit).
   **Expect:** new event in the log within ~2 s; workers with open tasks walk
   to their task bins (real FWMS Task coordinates) with task bubbles.
3. Controls: **Pause** freezes walking; **Speed** scales it; **Release
   Continuous/Waves** switches the live setting (KPI card updates); **Labels**
   All/Scans/Off.
4. `+ Inbound truck` → an idle inbound door goes **Busy** with a trailer id
   and a check-in event. `+ 20 orders` → creates **20 real Sales Orders**
   (Continuous mode releases waves + pick tasks immediately — watch the pick
   queue and Orders & Picking feed).
5. Filters: the log dropdown mirrors the 10 reference filters; the 11-step
   **tour** highlights each zone, selects a matching worker and pre-filters
   the log per step.
6. Click any worker (canvas or roster chips) → inspector shows role, current
   task and why, scans today, tasks done, recent scans.
7. KPI cards refresh every ~5 min via the scheduler
   (`bench --site fwms.localhost execute fwms.dev.check_kpis` to force).
8. **Simulation mode**: FWMS Settings → ✅ Simulation Mode → synthetic demo
   events via `fwms.api.floorview.simulate_tick` (wire a button or `bench
   execute`). Turn it off after.

---

## 13. Export documentation pack

1. Desk → FWMS Consignment → New: Transport Mode **Road**, carrier e.g.
   `Nordic Haulage`, Incoterms `FOB`, places of loading/delivery, add the DN
   shipped in §8 → Save.
2. **Print** → format **Commercial Invoice (Export)**.
   **Expect:** line-level **HS code** (e.g. 1806.90) and **country of origin**
   from the Item master, incoterms, totals.
3. Print **Certificate of Origin** and **Export Packing List (Carton)**.
   **Expect:** CoO lists origin per line; the packing list shows **carton-level**
   contents from the shipped LPNs.
4. Also print a **FWMS Packing Slip** from any Packing Slip (§7).

---

## 14. Permissions spot-check

1. Desk login as `supervisor@fwms.local` → FWMS workspace visible, all doctypes
   writable; can submit Stock Reconciliations.
2. `/rf` as `W003/0003` — RF works; the same user in Desk (`w003@fwms.local`,
   set a password via `bench --site <site> set-password w003@fwms.local <pwd>`)
   sees **no** FWMS write access beyond operator rights and no SR submit.

---

## 15. PWA / hardware checks

1. Chrome/Edge → `/rf` → menu → *Install app* (manifest served from
   `/api/method/fwms.api.pwa.manifest`).
2. DevTools → Application → Service Worker: `service_worker` registered with
   scope `/rf`; take the terminal offline → shell still loads (data calls fail
   gracefully with toasts).
3. Keyboard-wedge scanner: focus any scan field, scan any printed barcode —
   terminates with Enter and submits.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `name 'fwms' is not defined` from `bench execute` | The called function raised; run `bench --site <site> execute fwms.dev.seed_with_traceback`-style wrappers or `bench console` for the real traceback |
| Hook/event changes not visible | `bench --site <site> clear-cache` |
| Pick queue empty | Create a Sales Order (§5) or re-seed a fresh site (appendix) |
| "Not permitted" on RF submit | `bench --site <site> migrate` re-applies FWMS roles/permissions (`fwms.setup.install.after_migrate`) |
| Port conflict on :8000 | `bench start` Procfile web port |

## Appendix — reset to a pristine demo floor

Reproducible fresh site (verified end-to-end):

```bash
bench new-site fwms-test2.local --mariadb-root-password <root-pw> --admin-password admin --install-app fwms
bench --site fwms-test2.local install-app erpnext
bench --site fwms-test2.local migrate
bench --site fwms-test2.local execute fwms.setup.install.setup_company
bench --site fwms-test2.local execute fwms.setup.seed_demo.run
bench --site fwms-test2.local set-password supervisor@fwms.local 'Supervisor#2026'
```

Or wipe FWMS data on an existing site (keep transactions!): not automated —
prefer the fresh-site route.
