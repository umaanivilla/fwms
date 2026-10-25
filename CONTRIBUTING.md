# Contributing to FWMS

Thanks for looking at FWMS! Contributions are welcome — bug reports, flow
fixes, docs, and the roadmap items below are all useful.

## Development setup

```bash
# inside a Frappe v16 bench with ERPNext v16 installed
bench get-app https://github.com/<you>/fwms      # or your fork
bench --site yoursite install-app fwms
bench --site yoursite migrate
bench --site yoursite execute fwms.setup.install.setup_company   # one-time bootstrap
bench --site yoursite execute fwms.setup.seed_demo.run           # demo data (optional but handy)
```

## Ground rules

- **ERPNext stays the system of record.** Stock truth lives in Stock Ledger
  Entries; FWMS LPN/bin state must always reconcile back to it. If your change
  posts documents, assert the ledger in tests.
- **No build step on the RF/floorview frontends.** Vanilla ES modules served
  straight from `fwms/public/` — keep it that way unless there is a very good
  reason.
- **Every RF endpoint gates on `require_operator()`** (or an explicit
  `frappe.only_for`). New endpoints need the same.
- Raw SQL must be parameterized; prefer `frappe.qb` / `frappe.get_all`.

## Tests & lint

```bash
bench --site yoursite run-tests --app fwms   # 17 integration tests, seeded site
pre-commit install                           # ruff, ruff-format, eslint, prettier
pre-commit run --all-files
```

`fwms/dev.py` has isolated smoke checks (`bench execute fwms.dev.check_receive`
etc.) if you want to exercise one flow without walking the RF UI.

## Where help is wanted

See the Roadmap section in the README — the Carrier/Customs/Automation gateway
stubs in `fwms/gateways/` are declared interfaces waiting for real adapters,
and the floor view geometry is begging to become data-driven.

## License

By contributing you agree that your contributions are licensed under the
GNU GPL v3, matching the project license.
