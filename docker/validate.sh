#!/usr/bin/env bash
# validate.sh — end-to-end Print Bridge validation script
#
# Prerequisites (must be already running):
#   docker compose up -d
#
# What this does:
#   1. Waits for HA, GreenMail, and CUPS to be healthy.
#   2. Injects a test email with a minimal PDF attachment via GreenMail SMTP.
#   3. Polls the HA REST API until sensor.auto_print_last_job = "success"
#      (or until timeout).
#   4. Asserts that cups-pdf wrote a PDF to the shared output volume.
#   5. Exits 0 on full success, 1 on any failure.
#
# Required env vars (or set in ha-config/secrets.yaml):
#   HA_TOKEN  — HA long-lived access token (create at /profile in HA UI)
#
# Usage:
#   cd docker/
#   HA_TOKEN=<token> ./validate.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HA_URL="${HA_URL:-http://localhost:8123}"
HA_TOKEN="${HA_TOKEN:-}"
GREENMAIL_SMTP="localhost:3025"
GREENMAIL_REST="http://localhost:8080"
CUPS_OUTPUT_DIR="${CUPS_OUTPUT_DIR:-./cups-output}"   # bind-mount or local copy
TIMEOUT_SECONDS=60
POLL_INTERVAL=3

SENDER="test@test.example.com"
RECEIVER="test@test.example.com"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

# ---------------------------------------------------------------------------
# 0. Prerequisites check
# ---------------------------------------------------------------------------
if [[ -z "$HA_TOKEN" ]]; then
    fail "HA_TOKEN is not set. Generate one at $HA_URL/profile → Long-Lived Access Tokens."
fi

for cmd in curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || fail "Required command not found: $cmd"
done

# ---------------------------------------------------------------------------
# 1. Wait for all services to be healthy
# ---------------------------------------------------------------------------
info "Waiting for HA..."
for i in $(seq 1 30); do
    if curl -sf "$HA_URL/api/" -H "Authorization: Bearer $HA_TOKEN" >/dev/null 2>&1; then
        pass "HA is ready."
        break
    fi
    [[ $i -eq 30 ]] && fail "HA did not become ready in time."
    sleep 2
done

info "Waiting for GreenMail..."
for i in $(seq 1 20); do
    if curl -sf "$GREENMAIL_REST/api/service/readiness" >/dev/null 2>&1; then
        pass "GreenMail is ready."
        break
    fi
    [[ $i -eq 20 ]] && fail "GreenMail did not become ready in time."
    sleep 2
done

info "Waiting for CUPS..."
for i in $(seq 1 20); do
    if curl -sf "http://localhost:631/" >/dev/null 2>&1; then
        pass "CUPS is ready."
        break
    fi
    [[ $i -eq 20 ]] && fail "CUPS did not become ready in time."
    sleep 2
done

# ---------------------------------------------------------------------------
# 2. Build a minimal but valid PDF in-memory and send via GreenMail SMTP
# ---------------------------------------------------------------------------
info "Injecting test email with PDF attachment..."

python3 - <<'PYEOF'
import smtplib
import base64
import io
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from pypdf import PdfWriter

# Build a one-page A4 PDF
writer = PdfWriter()
writer.add_blank_page(width=595, height=842)
buf = io.BytesIO()
writer.write(buf)
pdf_bytes = buf.getvalue()

msg = MIMEMultipart()
msg["From"] = "test@test.example.com"
msg["To"]   = "test@test.example.com"
msg["Subject"] = "Print Bridge validation job"
msg.attach(MIMEText("Please print the attached file.", "plain"))

part = MIMEApplication(pdf_bytes, _subtype="pdf", Name="validation_test.pdf")
part["Content-Disposition"] = 'attachment; filename="validation_test.pdf"'
msg.attach(part)

with smtplib.SMTP("localhost", 3025) as s:
    s.sendmail(msg["From"], [msg["To"]], msg.as_bytes())

print("Email injected successfully.")
PYEOF

pass "Test email sent."

# ---------------------------------------------------------------------------
# 3. Poll HA REST API for sensor.auto_print_last_job = "success"
# ---------------------------------------------------------------------------
info "Waiting up to ${TIMEOUT_SECONDS}s for Print Bridge to process the job..."

ENTITY="sensor.auto_print_last_job"
ELAPSED=0
JOB_STATE=""

while [[ $ELAPSED -lt $TIMEOUT_SECONDS ]]; do
    JOB_STATE=$(
        curl -sf \
            -H "Authorization: Bearer $HA_TOKEN" \
            "$HA_URL/api/states/$ENTITY" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('state',''))" \
        2>/dev/null || echo ""
    )

    if [[ "$JOB_STATE" == "success" ]]; then
        pass "sensor.auto_print_last_job = success"
        break
    fi

    if [[ "$JOB_STATE" == "failed" ]]; then
        ATTRS=$(
            curl -sf \
                -H "Authorization: Bearer $HA_TOKEN" \
                "$HA_URL/api/states/$ENTITY" \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('attributes',{}))" \
            2>/dev/null || echo ""
        )
        fail "Print job failed. Attributes: $ATTRS"
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$(( ELAPSED + POLL_INTERVAL ))
done

[[ "$JOB_STATE" != "success" ]] && \
    fail "Timed out after ${TIMEOUT_SECONDS}s. Last sensor state: '${JOB_STATE}'"

# ---------------------------------------------------------------------------
# 4. Assert cups-pdf wrote a PDF file
# ---------------------------------------------------------------------------
info "Checking CUPS output volume for printed PDF..."

# The volume is named cups_output; inspect it via the cups container.
PDF_COUNT=$(docker exec auto_print_cups \
    sh -c 'ls /var/spool/cups-pdf/ANONYMOUS/*.pdf 2>/dev/null | wc -l' || echo "0")

if [[ "$PDF_COUNT" -gt 0 ]]; then
    pass "cups-pdf produced $PDF_COUNT PDF file(s) in the output volume."
else
    fail "No PDF found in cups-pdf output directory. Check CUPS logs: docker logs auto_print_cups"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  All validation checks passed!${NC}"
echo -e "${GREEN}========================================${NC}"
echo "  Email injected → HA IMAP → print_bridge → CUPS → cups-pdf"
