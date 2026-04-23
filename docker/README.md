# Print Bridge — Docker Validation Stack

Self-contained stack for end-to-end testing **without** a real IMAP account or physical printer.

| Service | Image | Purpose |
|---|---|---|
| `homeassistant` | `ghcr.io/home-assistant/home-assistant:stable` | HA with print_bridge mounted |
| `greenmail` | `greenmail/standalone:2.0.0` | Local IMAP + SMTP server |
| `cups` | Built from `./cups/Dockerfile` | CUPS + cups-pdf virtual printer |

`cups-pdf` saves every print job as a PDF to a Docker volume, which `validate.sh` inspects to confirm delivery.

---

## Quick Start

### 1. Start the stack

```bash
cd docker/
docker compose up -d --build
```

First run takes a few minutes to pull images and build the CUPS image.

### 2. Finish HA onboarding

Open [http://localhost:8123](http://localhost:8123) and complete the wizard.

### 3. Add the HA IMAP integration

> Settings → Integrations → Add → **IMAP**

| Field | Value |
|---|---|
| Server | `localhost` |
| Port | `3143` |
| SSL | Off |
| Username | `test@test.example.com` |
| Password | `test` |

In **advanced mode** → disable IMAP-Push (GreenMail does not support IDLE — use polling).

### 4. Add the Print Bridge integration

> Settings → Integrations → Add → **Print Bridge**

| Field | Value |
|---|---|
| CUPS Base URL | `http://localhost:631` |
| Printer Name | `TestPrinter` |

Then open **Configure** (options) and add `test@test.example.com` to Allowed Senders.

### 5. Generate a HA token

> [http://localhost:8123/profile](http://localhost:8123/profile) → Long-Lived Access Tokens → **Create Token**

### 6. Run the validation script

```bash
cd docker/
HA_TOKEN=<your-token> ./validate.sh
```

Expected output:

```
[PASS] HA is ready.
[PASS] GreenMail is ready.
[PASS] CUPS is ready.
[PASS] Test email sent.
[PASS] sensor.auto_print_last_job = success
[PASS] cups-pdf produced 1 PDF file(s) in the output volume.

========================================
  All validation checks passed!
========================================
  Email injected → HA IMAP → print_bridge → CUPS → cups-pdf
```

---

## Stopping the stack

```bash
docker compose down -v   # -v removes the cups_output volume too
```

---

## Inspecting results

```bash
# See printed PDFs
docker exec auto_print_cups ls /var/spool/cups-pdf/ANONYMOUS/

# CUPS logs
docker logs auto_print_cups

# HA logs (filter to auto_print)
docker logs auto_print_ha 2>&1 | grep auto_print
```
