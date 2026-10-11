#!/usr/bin/env bash
# RF Receiving proof-of-concept driven entirely through frappectl (the Frappe
# REST API v2 CLI): token-authenticated as operator W001, verified read-only
# as the supervisor. Same flow as `bench --site fwms.localhost execute
# fwms.dev.check_receive`, but over HTTP like a real RF gun or integration
# client would.
#
# Prereqs (see MANUAL_TESTING.md, "CLI testing with frappectl"):
#   - frappectl installed:        uv tool install frappectl
#   - bench running on :8000      bash start-fwms.sh
#   - $BENCH/config/frappectl-ro.env and frappectl-rw.env (chmod 600)
#
# Exit codes: 0 pass · 1 fail · 2 no pending Purchase Order on the site

set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RO_ENV="$BENCH_DIR/config/frappectl-ro.env"
RW_ENV="$BENCH_DIR/config/frappectl-rw.env"
FR="${FR_BIN:-fr}"

# Profile wrappers: each fr call runs in a subshell with that profile's token
# env sourced. Read-only also sets FRAPPE_READ_ONLY=1, so frappectl refuses
# POSTs before they leave this machine.
ro() { (set -a; . "$RO_ENV"; set +a; exec "$FR" "$@"); }
rw() { (set -a; . "$RW_ENV"; set +a; exec "$FR" "$@"); }

step() { printf '\n=== %s\n' "$*"; }
fail=0
check() {  # check <label> <ok:0|1> <detail>
	if [ "$2" = 0 ]; then printf '  PASS %s — %s\n' "$1" "$3"
	else printf '  FAIL %s — %s\n' "$1" "$3"; fail=1; fi
}
die() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

command -v "$FR" >/dev/null 2>&1 || die "fr not found — install with: uv tool install frappectl"
[ -f "$RO_ENV" ] && [ -f "$RW_ENV" ] || die "missing $BENCH_DIR/config/frappectl-{ro,rw}.env — see MANUAL_TESTING.md"
command -v python3 >/dev/null 2>&1 || die "python3 is required for JSON parsing"

step "0. Connectivity and identities"
RO_USER="$(ro auth whoami 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["user"])')" \
	|| die "read-only profile failed — is the bench running on :8000?"
RW_USER="$(rw auth whoami 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["user"])')" \
	|| die "operator profile failed — check frappectl-rw.env"
echo "read-only: $RO_USER   rf-operator: $RW_USER"

ME="$(rw method call fwms.api.rf_auth.me 2>/dev/null)" \
	|| die "rf_auth.me rejected the token — is $RW_USER an active FWMS worker with the operator role?"
WORKER="$(printf '%s' "$ME" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["worker_code"],d["person_name"])')"
echo "RF identity: $WORKER"

step "1. Finding a submitted Purchase Order with pending quantity (operator)"
PO_LIST="$(rw doc list "Purchase Order" -f docstatus=1 --fields name,supplier --order-by 'creation asc' --limit 25 2>/dev/null)" \
	|| die "doc list Purchase Order failed"
PO=""; ITEM=""; PENDING=""
while read -r po; do
	[ -n "$po" ] || continue
	info="$(rw method call fwms.api.rf_receiving.po_info -F po="$po" 2>/dev/null)" || continue
	pick="$(printf '%s' "$info" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if d.get("fully_received"):
    sys.exit(1)
for it in d["items"]:
    if it["pending_qty"] >= 3:
        print(it["item_code"], int(it["pending_qty"]))
        break
else:
    sys.exit(1)
')" || continue
	PO="$po"; ITEM="${pick% *}"; PENDING="${pick##* }"
	break
done < <(printf '%s' "$PO_LIST" | python3 -c 'import sys,json
for r in json.load(sys.stdin): print(r["name"])')

if [ -z "$PO" ]; then
	echo "No submitted Purchase Order has >=3 units pending."
	echo "Create one (MANUAL_TESTING.md §2): Desk > Buying > Purchase Order > New"
	echo "(supplier Cocoa West GmbH, item COCOA-100 qty 20) and submit, then rerun."
	exit 2
fi
echo "PO $PO · receiving 3 x $ITEM (${PENDING} pending)"

step "2. Committing receipt (operator, writable profile)"
LINES="[{\"item_code\":\"$ITEM\",\"qty\":3,\"qc\":\"Pass\"}]"
OUT="$(rw method call fwms.api.rf_receiving.commit_receipt -F po="$PO" -F lpn_barcode= -F "lines:=$LINES" 2>/dev/null)" \
	|| die "commit_receipt failed — rerun manually with --debug"
read -r PR LPN STATUS <<EOF
$(printf '%s' "$OUT" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["purchase_receipt"],d["lpn"],d["status"])')
EOF
echo "Purchase Receipt $PR · LPN $LPN · status: $STATUS"
case "$STATUS" in
	received) echo "  (fresh receipt)" ;;
	already_received) echo "  (idempotent re-run — same PO/LPN pair already received)" ;;
	*) die "unexpected commit_receipt status: $STATUS" ;;
esac

step "3. Verifying (FWMS state as supervisor, ERPNext docs as operator)"
# RBAC note: Purchase Order/Receipt reads belong to the FWMS Operator role
# (fwms.setup.install.ERP_PERMISSIONS); the FWMS Supervisor profile owns the
# FWMS doctypes below — so each check uses the identity that should see it.
LPN_DOC="$(ro doc get "FWMS LPN" "$LPN" 2>/dev/null)" || die "doc get FWMS LPN $LPN failed"
read -r LPN_STATUS LPN_QTY <<<"$(printf '%s' "$LPN_DOC" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d["status"], int(sum(r["qty"] for r in d.get("items", []))))
')"
check "LPN status Receiving" $([ "$LPN_STATUS" = "Receiving" ]; echo $?) "$LPN is $LPN_STATUS"
check "LPN carries the received qty" $([ "$LPN_QTY" -ge 3 ]; echo $?) "$LPN holds $LPN_QTY units"

PR_DOC="$(rw doc get "Purchase Receipt" "$PR" 2>/dev/null)" || die "doc get Purchase Receipt $PR failed"
PR_DS="$(printf '%s' "$PR_DOC" | python3 -c 'import sys,json;print(json.load(sys.stdin)["docstatus"])')"
check "Purchase Receipt submitted" $([ "$PR_DS" = "1" ]; echo $?) "$PR docstatus=$PR_DS"

TASK_OK="$(ro doc list "FWMS Task" -f task_type=Putaway -f status=Open -f "lpn=$LPN" --fields name --limit 5 2>/dev/null \
	| python3 -c 'import sys,json;print(1 if json.load(sys.stdin) else 0)')"
check "Open Putaway task queued" $([ "$TASK_OK" = "1" ] || [ "$STATUS" = "already_received" ]; echo $?) \
	"$STATUS receipt → putaway task for $LPN"

EVENT="$(ro doc list "FWMS Event Log" -f "reference_doctype=Purchase Receipt" -f "reference_name=$PR" \
	--fields category,message --limit 1 2>/dev/null \
	| python3 -c 'import sys,json;r=json.load(sys.stdin);print(r[0]["message"] if r else "")')"
check "Event log entry" $([ -n "$EVENT" ]; echo $?) "${EVENT:-no Receiving & QC event found for $PR}"

step "4. Result"
if [ "$fail" = 0 ]; then
	echo "PASS — RF receiving flow worked end-to-end over the REST API."
	echo "      PR $PR · LPN $LPN · next: putaway (rf_putaway) is queued as a task."
else
	echo "FAIL — see the failed checks above."
fi
exit "$fail"
