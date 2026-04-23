# Auto Print

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-85%20passing-brightgreen.svg)](tests/)

Print PDF email attachments directly to a network printer — fully inside Home Assistant.

**Auto Print** bridges HA's built-in IMAP integration with a CUPS print server.
When an email with a PDF attachment arrives from a matching sender (and folder),
the component fetches the bytes via `imap.fetch_part`, optionally reorders pages
for booklet printing, and sends an IPP/2.0 `Print-Job` directly to CUPS.

---

## Features

| | |
|---|---|
| **Event-driven** | Triggered instantly by HA's IMAP push (IDLE) — no polling |
| **Smart setup** | Auto-discovers CUPS printers; pre-fills sender from existing IMAP accounts |
| **Sender filter** | Accept only specific email addresses, or leave empty to accept all |
| **Folder filter** | Accept only emails arriving in specific IMAP folders (e.g. `INBOX/Print`) |
| **Duplex control** | One-sided, long-edge (portrait), or short-edge (landscape) per job or globally |
| **Booklet printing** | Automatic saddle-stitch page reordering for filenames matching a pattern |
| **Audit log** | Every print job fires `auto_print_job_completed` → appears in HA Logbook |
| **Job history sensor** | Last 50 jobs with sender, duplex, timestamp as attributes |
| **Filter preview** | Press a button to scan the mailbox and see which emails would be printed |
| **Blueprint** | Advanced per-sender/per-keyword rules with folder, duplex, and booklet logic |
| **Lovelace dashboard** | Ready-made audit view — job table, filter preview, statistics |
| **Services** | `print_file`, `clear_queue`, `process_imap_part`, `check_filter` |

---

## Prerequisites

### 1. HA IMAP Integration (built-in)

> Settings → Devices & Services → Add Integration → **IMAP**

Configure it with your mail server details.
Auto Print listens to the `imap_content` events it fires — **no credentials are stored in Auto Print**.

### 2. CUPS Print Server

The recommended option for HA OS is the **[CUPS add-on by peternicholls](https://github.com/peternicholls/ha-cups-addon)**:

1. **Settings → Add-ons → Add-on Store** → add repository `https://github.com/peternicholls/ha-cups-addon`
2. Install **CUPS Print Server** and start it.
3. Open the CUPS web UI at `http://<ha-host>:631` → add your printer.
4. Note the **queue name** shown in the CUPS Printers list.

---

## Installation

### HACS (recommended)

1. **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/rubeecube/custom_component_hassio_print` — category **Integration**
3. Search for **Auto Print** → install → restart HA.

### Manual

Copy `custom_components/auto_print/` into your HA config directory and restart:

```
/config/custom_components/auto_print/
```

---

## Configuration

### Step 1 — HA IMAP Integration

Configure the built-in IMAP integration first (see [Prerequisites](#1-ha-imap-integration-built-in)).

### Step 2 — Add Auto Print

> Settings → Devices & Services → Add Integration → **Auto Print**

The setup wizard auto-discovers both **CUPS** and **direct IPP printers** at common addresses and lists any IMAP accounts already configured in HA.

#### Option A — Direct IPP (no CUPS required)

For any AirPrint / IPP-capable printer on your network (including most Canon PIXMA with AirPrint), you can print directly without CUPS:

| Field | Example |
|---|---|
| Direct IPP Printer URL | `http://10.0.0.23/ipp/print` or `ipp://10.0.0.23/ipp/print` |

The integration sends an IPP `Print-Job` packet containing a PDF directly to the printer. No conversion layer is needed — modern AirPrint printers accept PDF natively. Leave the CUPS fields empty.

#### Option B — Via CUPS

| Field | Example | Description |
|---|---|---|
| CUPS Base URL | `http://10.0.0.23:631` | Auto-filled if CUPS is found; edit if on a different host |
| Printer Name | `Canon_MG3600_series` | Select from discovered printers, or choose **Enter name manually…** |

Use CUPS when: the printer is USB-attached, needs driver/raster conversion, or you want a managed print queue.

#### Common to both modes

| Field | Description |
|---|---|
| Pre-fill Senders from | Optional — pre-loads an IMAP account's address into Allowed Senders |

### Options (editable any time)

> Settings → Devices & Services → Auto Print → **Configure**

The form shows a live hint: *"Your IMAP integrations monitor: INBOX (print@example.com)"* so you know which exact folder names to use.

| Option | Default | Description |
|---|---|---|
| **Allowed Senders** | *(empty = all)* | One email address per line. Empty accepts mail from any sender. |
| **IMAP Folder Filter** | *(empty = all)* | One folder name per line. Empty accepts mail from any folder. Use the exact name shown in the hint above (e.g. `INBOX`, `INBOX/Print`). |
| **Default Duplex Mode** | Two-sided portrait | Fallback for all jobs. |
| **Booklet Patterns** | *(empty)* | Filename substrings that trigger booklet page reordering. |
| **Delete after printing** | On | Remove the PDF from the queue folder after a successful print. |
| **Print Queue Folder** | `/media/print_queue` | Used by the `print_file` service and queue-depth sensor. |
| **Email Action after Printing** | Do nothing | What to do with the email after the PDF prints: **Do nothing** / **Mark as read** / **Move to archive folder** / **Delete from server**. |
| **Archive Folder** | `INBOX/Printed` | Target folder when "Move to archive folder" is selected. Created automatically by most IMAP servers. |
| **Notify when print fails** | On | Send a HA persistent notification when a job fails (with error details). |
| **Notify when print succeeds** | Off | Send a HA persistent notification when a job completes successfully. |

---

## Entities

| Entity | Type | State | Key attributes |
|---|---|---|---|
| `sensor.auto_print_*_print_queue_depth` | Sensor | File count | — |
| `sensor.auto_print_*_last_print_job` | Sensor | `success` / `failed` | `last_filename`, `last_status`, `sender`, `duplex`, `booklet`, `timestamp` |
| `sensor.auto_print_*_job_log` | Sensor | Total jobs sent | `jobs[]` — last 50 print attempts with full metadata |
| `sensor.auto_print_*_filter_preview` | Sensor | Matching emails with PDF | `emails[]`, `checked_at`, `imap_account`, `total_found`, `matching_filter`, `with_pdf` |
| `binary_sensor.auto_print_*_printer_online` | Binary Sensor | `on` / `off` | — |
| `button.auto_print_*_print_test_page` | Button | — | Sends a built-in one-page PDF to the printer |
| `button.auto_print_*_check_filter` | Button | — | Scans the mailbox and updates `filter_preview` sensor |

*`*` is a slug derived from the printer's CUPS queue name.*

---

## Services

### `auto_print.print_file`

Print a PDF from the HA filesystem.

| Field | Required | Description |
|---|---|---|
| `file_path` | yes | Absolute path to the PDF |
| `duplex` | no | Override duplex for this job |
| `booklet` | no | Force booklet page reordering |

### `auto_print.clear_queue`

Delete all `.pdf` files from the configured queue folder.

### `auto_print.process_imap_part`

Fetch a specific IMAP attachment and print it.
Used internally by the blueprint; callable from any automation or script.

| Field | Required | Description |
|---|---|---|
| `entry_id` | yes | IMAP config entry ID (`trigger.event.data.entry_id`) |
| `uid` | yes | Email UID (`trigger.event.data.uid`) |
| `part_key` | yes | Part key from `trigger.event.data.parts` |
| `filename` | no | Display name for the print job |
| `duplex` | no | Override duplex for this job |
| `booklet` | no | Force booklet page reordering |

### `auto_print.check_filter`

Connect to IMAP and list emails that match the current filter settings.
Returns a service response and updates `sensor.auto_print_*_filter_preview`.

| Field | Required | Description |
|---|---|---|
| `imap_entry_id` | no | Which IMAP entry to query (defaults to first configured) |

---

## Audit Log

Every print attempt fires an `auto_print_job_completed` event to the HA event bus.
This event appears in the native HA **Logbook** as a human-readable sentence:

> **Auto Print** — Printed `invoice.pdf`  ·  two-sided-long-edge  ·  from billing@example.com  ·  Canon_MG3600_series

> **Auto Print** — Print failed for `bad.pdf`: HTTP 503  ·  from sender@example.com

The `sensor.auto_print_*_job_log` entity stores the last 50 jobs as attributes,
including timestamp, filename, success/failure, sender, duplex mode, and booklet flag.

---

## How it works

```
Mail Server ──IMAP IDLE──► HA IMAP Integration ──imap_content event──► Auto Print
                                                                              │
                          1. Check: is sender in allowed_senders?             │
                          2. Check: is folder in folder_filter?               │
                          3. For each PDF part → imap.fetch_part ◄────────────┘
                          4. Booklet? → reorder pages
                          5. Build IPP/2.0 packet → POST to CUPS
                          6. Fire auto_print_job_completed → Logbook
```

---

## Blueprint — Advanced Per-Email Rules

For fine-grained per-sender or per-keyword rules (different duplex per sender,
booklet for some subjects, multiple printers), use the included automation blueprint.

### Import

**Settings → Automations → Blueprints → Import Blueprint** — paste:

```
https://github.com/rubeecube/custom_component_hassio_print/blob/main/blueprints/automation/auto_print/print_from_email.yaml
```

### Inputs

| Input | Default | Description |
|---|---|---|
| IMAP Account | — | Which IMAP integration entry to monitor |
| Allowed Senders | *(all)* | Comma-separated addresses |
| IMAP Folder Filter | *(all)* | Comma-separated folder names |
| Subject Must Contain | *(all)* | Optional keyword gate on the subject line |
| Default Duplex Mode | Two-sided portrait | Fallback for all jobs |
| One-sided Keywords | — | Subject keywords that override to one-sided |
| Two-sided Portrait Keywords | — | Subject keywords that force long-edge duplex |
| Booklet Keywords | — | Subject keywords that trigger booklet reordering |
| Booklet Senders | — | Senders whose mail is always printed as a booklet |
| Mark as Seen | On | Set `\Seen` flag on the email after processing |

### Decision logic

```
imap_content event received
        │
        ├─ folder in folder_filter? (empty = yes) ──── no ──► skip
        │
        ├─ sender in allowed_senders? (empty = yes) ── no ──► skip
        │
        ├─ subject contains filter? (empty = yes) ───── no ──► skip
        │
        ├─ any PDF part? ────────────────────────────── no ──► skip
        │
        └─ for each PDF part:
                 is_booklet = subject has booklet keyword
                              OR sender in booklet_senders
                 duplex     = two-sided-short-edge  if is_booklet
                            = one-sided             if subject has one-sided keyword
                            = two-sided-long-edge   if subject has two-sided keyword
                            = default_duplex        otherwise
                 ──► auto_print.process_imap_part(duplex, booklet)
                 ──► imap.seen  (if mark_as_seen)
```

### Example — multiple rule sets

**Automation 1 — Invoices (one-sided, INBOX):**
- Folder Filter: `INBOX`
- Allowed Senders: `billing@mybank.com`
- One-sided Keywords: `INVOICE`

**Automation 2 — Programmes (booklet, INBOX/Print):**
- Folder Filter: `INBOX/Print`
- Booklet Keywords: `Programme, Bulletin`
- Default Duplex: Two-sided landscape

---

## Lovelace Audit Dashboard

A ready-made dashboard view is included at [`lovelace/auto_print_audit.yaml`](lovelace/auto_print_audit.yaml).

Add it as a new view to any dashboard (**Edit → Add View → Manual Configuration (YAML)**).
Replace `PRINTER_NAME` with the slug for your printer queue name.

The view includes:

- **Status row** — printer online, queue depth, jobs sent, last job status
- **Logbook card** — 7-day print event history
- **Recent jobs table** — last 50 jobs: timestamp / filename / ✅❌ / sender / duplex / booklet
- **Filter Preview table** — folder / subject / sender / ✅match / PDF count
- **Statistics graph** — daily job counts (30 days)
- **Action buttons** — Check Filter, Print Test Page, Clear Queue

---

## Developer Setup

```bash
git clone https://github.com/rubeecube/custom_component_hassio_print
cd custom_component_hassio_print
chmod +x setup-dev.sh && ./setup-dev.sh
```

`setup-dev.sh` activates git hooks (pre-commit + pre-push author checks), sets a
repo-local git identity, and creates the Python venv.

```bash
# Run tests
./venv/bin/pytest tests/ -v

# Validation stack (HA + GreenMail IMAP + CUPS with cups-pdf)
cd docker && docker compose up -d --build
HA_TOKEN=<token> ./validate.sh
```

---

## FAQ

**Q: Do I need to configure IMAP credentials in Auto Print?**  
A: No. Credentials live in HA's IMAP integration only. Auto Print subscribes to the events it fires.

**Q: Can I print from multiple email addresses?**  
A: Yes — add one address per line in **Allowed Senders**, or leave it empty to accept all.

**Q: Can I filter by mailbox folder?**  
A: Yes — add one folder name per line in **IMAP Folder Filter** (e.g. `INBOX/Print`). The hint in the Options form shows the exact folder names your IMAP integration monitors. Leave empty to accept all folders.

**Q: How do I check which emails would be printed right now?**  
A: Press the **Check Filter** button (or call `auto_print.check_filter`). Results appear in the `sensor.auto_print_*_filter_preview` entity and in the Lovelace audit view.

**Q: What if my PDF is very large?**  
A: Auto Print calls `imap.fetch_part` which has no size limit (unlike the 32 KB event body limit).

**Q: How do I enable booklet printing?**  
A: Add a substring of the PDF filename to **Booklet Patterns** in the options (e.g. `Programme`). Any attachment whose filename contains that string is automatically reordered for saddle-stitch printing.

**Q: What happens to the email after it's printed?**  
A: Configure **Email Action after Printing** in Options: **Mark as read** keeps it in your inbox but removes the unread badge; **Move to archive folder** moves it to `INBOX/Printed` (or a folder you choose) and marks it read; **Delete from server** removes it permanently. Default is **Do nothing** (no change to the email).

**Q: How do I get notified when a print job fails?**  
A: **Notify when print fails** is enabled by default. A HA persistent notification will appear in the bell (🔔) menu with the filename and error. Enable **Notify when print succeeds** if you also want confirmation of successful prints.

**Q: Do I need CUPS at all?**  
A: Not for modern AirPrint printers. If your printer has an IPP endpoint (most WiFi printers made after ~2015 do), use **Direct IPP mode** with a URL like `http://10.0.0.23/ipp/print`. CUPS is useful when the printer is USB-attached, needs format conversion (PCL/PostScript), or you want a managed queue.

**Q: How do I find my printer's IPP URL?**  
A: Common paths: `http://printer-ip/ipp/print`, `http://printer-ip:631/ipp/print`. Your router's device list shows the printer's IP. For Canon PIXMA: the embedded server is usually at port 80 with path `/ipp/print`.

**Q: CUPS is on a different host. What URL do I use?**  
A: `http://<cups-host-ip>:631`. When using the CUPS add-on on HA OS with host networking, the host IP is your HA LAN address.

---

## Contributing

Pull requests are welcome. Please open an issue first for significant changes.

```bash
./venv/bin/pytest tests/ -v   # 85 tests, no external dependencies required
```

---

## License

[MIT](LICENSE) © 2026 rubeecube
