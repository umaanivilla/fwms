# Changelog

All notable changes to FWMS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
semantic-ish (major.minor.patch, pre-1.0 expect breakage).

## [0.1.0] - 2026-10-25

First public release.

### Added
- RF terminal PWA (`/rf`): PIN worker login, receiving (PO → Purchase Receipt
  → LPN, QC pass/fail routing), putaway with slotting suggestions, bin-directed
  FEFO picking with cart LPNs, packing with ZPL case labels, trailer load/ship,
  blind & expected cycle counting, replenishment, returns grading, scrap, and
  a system-suggestion dropdown on every scan field.
- Floor view dashboard (`/floorview`): canvas zone map, live KPIs, event
  stream, simulation tick, guided tour.
- Task engine: self-assignment queue with skill filters, priorities, and
  per-worker state.
- Wave management: Continuous and cutoff-based release building ERPNext Pick
  Lists ordered FEFO/FIFO from pick-face LPN stock.
- LPN container tracking (Tote/Carton/Pallet/Cart) reconciled against the
  ERPNext stock ledger.
- Export documentation: FWMS Consignment with Commercial Invoice, Certificate
  of Origin, and Export Packing List print formats.
- Count programs (ABC-driven scheduling), KPI snapshots (5-minute rollups),
  and the FWMS Event Log floor event stream.
- Deterministic demo seeder (26 workers, 72 bins, staggered batch expiries,
  open POs/SOs) and a manual acceptance test script.
- Integration test suite covering receiving, waves, counting, QC/scrap and the
  full order-to-ship cycle.
