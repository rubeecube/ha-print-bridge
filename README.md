# Print Bridge

<p align="center">
  <img src="https://raw.githubusercontent.com/rubeecube/ha-print-bridge/main/hacs.png" alt="Print Bridge logo" width="480"/>
</p>

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.4%2B-blue.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-167%20passing-brightgreen.svg)](tests/)
[![Version](https://img.shields.io/badge/version-0.1.23-blue.svg)](https://github.com/rubeecube/ha-print-bridge/releases)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository?owner=rubeecube&repository=ha-print-bridge&category=integration)
[![Add Print Bridge to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=print_bridge)

Print common email attachments directly to a network printer — fully inside Home Assistant.

### Supported platforms

| Platform | Supported | Notes |
|---|:---:|---|
| **Home Assistant OS** | ✅ | Recommended — full mDNS discovery, all features |
| **Home Assistant Supervised** | ✅ | Full support on any Linux host |
| **Home Assistant Container** (`--network=host`) | ✅ | Host networking required for mDNS |
| **Home Assistant Core** (Linux) | ✅ | mDNS works if the host has Avahi/Bonjour |
| **Docker (bridge network)** | ⚠️ | mDNS discovery limited — type IPP URL manually |

> mDNS printer discovery runs on the HA host and requires multicast packets to reach the HA process.
> Docker bridge networking blocks multicast, so auto-discovery will not find LAN printers.
> You can still configure a printer manually by typing its IPP URL.

**Print Bridge** bridges HA's built-in IMAP integration with a CUPS print server.
When an email with printable attachments arrives from a matching sender (and folder),
the component fetches the bytes via `imap.fetch_part`, converts non-PDF files to
an internal PDF, optionally reorders pages for booklet printing, and sends an
IPP/2.0 `Print-Job` directly to CUPS or the printer.

---

## Features

| | |
|---|---|
| **Event-driven** | Triggered instantly by HA's IMAP push (IDLE) — no polling |
| **Smart setup** | Auto-discovers CUPS printers; pre-fills sender from existing IMAP accounts |
| **Sender filter** | Accept only specific email addresses, or leave empty to accept all |
| **Folder filter** | Accept only emails arriving in specific IMAP folders (e.g. `INBOX/Print`) |
| **Common document support** | Prints PDF, DOCX/DOCM, ODT, RTF, TXT, HTML, Markdown, XLS/XLSX/ODS/CSV/TSV, PPTX/ODP, and common image files |
| **Combined email jobs** | Converts and merges matching attachments from one email into one print job for faster batches |
| **Duplex control** | One-sided, long-edge (portrait), or short-edge (landscape) per job or globally |
| **Booklet printing** | Automatic saddle-stitch page reordering for filenames matching a pattern |
| **Audit log** | Every print job fires `print_bridge_job_completed` → appears in HA Logbook |
| **Job history sensor** | Last 50 jobs with sender, duplex, timestamp as attributes |
| **Filter preview** | Press a button to scan the mailbox and see which emails would be printed |
| **One-press mail printing** | Dashboard buttons print the latest five matching mailbox emails |
| **Mailbox/printer selectors** | Dashboard selects choose which IMAP account to scan and which printer receives manual jobs |
| **Dashboard configuration** | Switch/select/text entities let Lovelace manage filters, duplex, cleanup, notifications, and schedule settings |
| **Scheduled printing** | Hold jobs outside allowed days, hours, or a custom HA template gate |
| **Queued job view/cancel** | Dashboard shows up to five scheduled jobs and can discard queued work before submission |
| **Blueprint** | Advanced per-sender/per-keyword rules with folder, duplex, and booklet logic |
| **Lovelace dashboard** | Paste-ready printer view plus detailed audit view |
| **Services** | `print_file`, `clear_queue`, `process_imap_message`, `process_imap_part`, `check_filter`, `print_email` |

---

## Prerequisites

### HA integrations — what is and isn't needed

| Integration | Required? | Role |
|---|:---:|---|
| **HA IMAP** (built-in) | **Yes** | Fires `imap_content` events when email arrives. Print Bridge listens to these. |
| **HA IPP** (built-in) | **No** | Monitors printer status (ink, paper, errors). Print Bridge does **not** use it — Print Bridge *is* its own IPP client and sends `Print-Job` packets directly. |
| **CUPS add-on** | Optional | Needed for USB printers or non-AirPrint printers that require driver conversion. AirPrint printers can be reached directly via IPP. |

> **Why not the HA IPP integration?**
> HA's built-in `ipp` integration ([docs](https://www.home-assistant.io/integrations/ipp/)) is a *monitoring* tool — it reads printer state, ink levels, and page counts.
> Print Bridge bypasses it entirely and speaks raw IPP/2.0 directly to the printer or CUPS server.
> You can install both side-by-side: use the HA IPP integration to monitor ink/status,
> and Print Bridge to receive and dispatch print jobs.

---

### 1. HA IMAP Integration (built-in) — **required**

> Settings → Devices & Services → Add Integration → **IMAP**

Configure it with your mail server details.
Print Bridge listens to the `imap_content` events it fires — **no credentials are stored in Print Bridge**.

### 2. Printer — choose one

#### Option A — Direct IPP (no extra software needed)

Any WiFi printer with AirPrint support (manufactured after ~2012) has a built-in IPP server.
Print Bridge can send jobs directly to the printer's endpoint, for example:

```
http://printer.local/ipp/print        (standard AirPrint path)
http://printer.local:631/ipp/print    (CUPS-style port)
```

No add-ons, no CUPS required. Set this URL in the **Direct IPP Printer URL** field during setup.

#### Option B — Via CUPS (for USB or legacy printers)

The recommended option for HA OS is the **[CUPS add-on by peternicholls](https://github.com/peternicholls/ha-cups-addon)**:

1. **Settings → Add-ons → Add-on Store** → add repository `https://github.com/peternicholls/ha-cups-addon`
2. Install **CUPS Print Server** and start it.
3. Open the CUPS web UI at `http://<ha-host>:631` → add your printer.
4. Note the **queue name** shown in the CUPS Printers list.

---

## Installation

### HACS (recommended)

Click the button below to open Print Bridge directly in HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository?owner=rubeecube&repository=ha-print-bridge&category=integration)

Or add it manually: **HACS → Integrations → ⋮ → Custom repositories** → add `https://github.com/rubeecube/ha-print-bridge` (category **Integration**) → search **Print Bridge** → install → restart HA.

### After installation — open the config flow

Once installed (via HACS or manually), click here to add the integration to your HA instance:

[![Add Print Bridge to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=print_bridge)

### Manual (without HACS)

Copy `custom_components/print_bridge/` into your HA config directory and restart:

```
/config/custom_components/print_bridge/
```

---

## Configuration

### Step 1 — HA IMAP Integration

Configure the built-in IMAP integration first (see [Prerequisites](#1-ha-imap-integration-built-in)).

### Step 2 — Add Print Bridge

> Settings → Devices & Services → Add Integration → **Print Bridge**

The setup wizard auto-discovers both **CUPS** and **direct IPP printers** at common addresses and lists any IMAP accounts already configured in HA.

#### Option A — Direct IPP (no CUPS required)

For any AirPrint / IPP-capable printer on your network (including most Canon PIXMA with AirPrint), you can print directly without CUPS:

| Field | Example |
|---|---|
| Direct IPP Printer URL | `http://printer.local/ipp/print` or `ipp://printer.local/ipp/print` |

The integration sends an IPP `Print-Job` packet directly to the printer. Modern
AirPrint printers usually accept PDF natively; if a direct IPP printer advertises
only PWG Raster or JPEG, Print Bridge converts the internal PDF to the accepted
format before submission. Leave the CUPS fields empty.

#### Option B — Via CUPS

| Field | Example | Description |
|---|---|---|
| CUPS Base URL | `http://cups.local:631` | Auto-filled if CUPS is found; edit if on a different host |
| Printer Name | `Canon_MG3600_series` | Select from discovered printers, or choose **Enter name manually…** |

Use CUPS when: the printer is USB-attached, needs driver/raster conversion, or you want a managed print queue.

#### Common to both modes

| Field | Description |
|---|---|
| Pre-fill Senders from | Optional — pre-loads an IMAP account's address into Allowed Senders |

### Options (editable any time)

> Settings → Devices & Services → Print Bridge → **Configure**

The form shows a live hint: *"Your IMAP integrations monitor: INBOX (print@example.com)"* so you know which exact folder names to use.

| Option | Default | Description |
|---|---|---|
| **Allowed Senders** | *(empty = all)* | One email address per line. Empty accepts mail from any sender. |
| **IMAP Folder Filter** | *(empty = all)* | One folder name per line. Empty accepts mail from any folder. Use the exact name shown in the hint above (e.g. `INBOX`, `INBOX/Print`). |
| **Default Duplex Mode** | Two-sided portrait | Fallback for all jobs. |
| **Booklet Patterns** | *(empty)* | Filename substrings that trigger booklet page reordering. |
| **Delete after printing** | On | Remove the PDF from the queue folder after a successful print. |
| **Print Queue Folder** | `/media/print_queue` | Used by the `print_file` service and queue-depth sensor. |
| **Raster DPI** | `150` | Used only when direct IPP printing must convert PDFs to PWG Raster/JPEG. Lower values are faster and create smaller jobs. |
| **Email Action after Printing** | Do nothing | What to do with the email after the PDF prints: **Do nothing** / **Mark as read** / **Move to archive folder** / **Delete from server**. |
| **Archive Folder** | `INBOX/Printed` | Target folder when "Move to archive folder" is selected. Created automatically by most IMAP servers. |
| **Notify when print fails** | On | Send a HA persistent notification when a job fails (with error details). |
| **Notify when print succeeds** | Off | Send a HA persistent notification when a job completes successfully. |
| **Reply to sender with print status** | Off | Send a status email to the original sender after printing. Requires an email-capable HA notify service. |
| **Status Reply Notify Service** | *(empty)* | Notify service used for status replies, for example `notify.smtp`. |
| **Enable print schedule** | Off | Hold matching jobs in the scheduled queue until the configured day, hour, and template gates are open. |
| **Print window start/end** | `07:00` / `22:00` | Allowed local print hours in 24-hour `HH:MM` format. Windows can wrap midnight. |
| **Print days** | *(every day)* | Optional weekday list. Use `mon`, `tue`, full names, or `1`-`7`; one per line or comma-separated. |
| **Print schedule template** | *(empty)* | Optional HA template. Jobs print only when it renders truthy (`true`, `on`, `yes`, or `1`). Available variables include `now`, `schedule_time`, `schedule_weekday`, `schedule_window_day`, `schedule_days`, `schedule_start`, `schedule_end`, and `printer_name`. |

---

## After Setup — Where to find everything

After configuring the integration you will see a notification:
**"Print Bridge — Action required"** — follow the links in it, then dismiss it.

### 1. Find your entities

> Settings → Devices & Services → Integrations → **Print Bridge** → click the device card

All sensors, buttons, and binary sensors appear here. You can press **Check Filter** directly from this page to scan your mailbox.

### 2. Add the management dashboard

For the easiest setup, copy `lovelace/printer_dashboard_template.yaml` from the repo into a new dashboard view:

> Your Dashboard → Edit → Add View → Manual configuration (YAML) → paste the file contents

Replace `YOUR_SLUG` with the entity slug shown in Developer Tools → States when you search "print\_bridge" (e.g. `canonmg3600series`).

The simple template includes component configuration controls, mailbox/printer selectors, status tiles, and print actions. The detailed audit view remains available at `lovelace/print_bridge_audit.yaml`.

### 3. Choose your printing mode

Use the dashboard **Auto Print** switch:

- **On** — simple setup; Print Bridge prints all PDFs from allowed senders automatically.
- **Off** — use the automation blueprint for per-sender / per-keyword rules.

### 4. Enable debug logging (troubleshooting)

Add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.print_bridge: debug
```

Then go to **Settings → System → Logs** to view debug output. This shows each discovery step, event received, and print job stage.

---

## Entities

| Entity | Type | State | Key attributes |
|---|---|---|---|
| `sensor.print_bridge_*_print_queue_depth` | Sensor | File count | — |
| `sensor.print_bridge_*_last_print_job` | Sensor | `success` / `failed` | `last_filename`, `last_status`, `sender`, `duplex`, `booklet`, `source_format`, `converted_format`, `attachments`, `skipped_attachments`, `timestamp` |
| `sensor.print_bridge_*_job_log` | Sensor | Total jobs sent | `jobs[]` — last 50 print attempts with full metadata |
| `sensor.print_bridge_*_filter_preview` | Sensor | Matching emails with printable attachments | `emails[]`, `checked_at`, `imap_account`, `total_found`, `matching_filter`, `with_pdf`, `with_printable` |
| `sensor.print_bridge_*_scheduled_queue` | Sensor | Queued job count | `jobs[]` shows up to 5 waiting jobs, plus `total_jobs`, `shown_jobs`, schedule settings |
| `binary_sensor.print_bridge_*_printer_online` | Binary Sensor | `on` / `off` | — |
| `select.print_bridge_*_imap_account` | Select | Selected IMAP account | Used by **Check Filter** and on-demand email printing when no account is specified |
| `select.print_bridge_*_target_printer` | Select | Selected printer | Used by dashboard print actions and default print services |
| `select.print_bridge_*_default_duplex_mode` | Select | Duplex mode | Updates the default duplex option |
| `select.print_bridge_*_email_action_after_print` | Select | Email action | Updates post-print mailbox cleanup behavior |
| `switch.print_bridge_*_*` | Switch | `on` / `off` | Auto-print, delete-after-printing, notifications, and schedule enablement |
| `text.print_bridge_*_*` | Text | Current value | Sender/folder filters, booklet patterns, queue/archive folders, and schedule fields |
| `button.print_bridge_*_print_test_page` | Button | — | Sends a built-in one-page PDF to the printer |
| `button.print_bridge_*_check_filter` | Button | — | Scans the mailbox and updates `filter_preview` sensor |
| `button.print_bridge_*_cancel_queued_jobs` | Button | — | Cancels schedule-held jobs and clears queued PDFs that have not been submitted |

*`*` is a slug derived from the printer's CUPS queue name.*

---

## Services

### Supported files

Print Bridge converts supported non-PDF inputs to an internal PDF before the
existing booklet, CUPS/direct IPP, raster, status reply, queue, and audit
pipeline runs. Conversion is best effort: readable content, basic tables, sheet
names, slide text, and image pages are preserved, but Office-perfect pagination,
macros, charts, embedded objects, headers/footers, and complex layout are not
guaranteed.

Supported: `.pdf`, `.docx`, `.docm`, `.odt`, `.rtf`, `.txt`, `.html`, `.htm`,
`.md`, `.xlsx`, `.xlsm`, `.xls`, `.ods`, `.csv`, `.tsv`, `.pptx`, `.odp`,
`.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, `.bmp`, `.webp`.

Explicitly unsupported: legacy binary `.doc` and `.ppt` files. These need an
external office engine such as LibreOffice, which Print Bridge does not bundle.

### `print_bridge.print_file`

Print a supported file from the HA filesystem. Non-PDF files are converted to an
internal PDF first.

| Field | Required | Description |
|---|---|---|
| `file_path` | yes | Absolute path to the supported file |
| `duplex` | no | Override duplex for this job |
| `booklet` | no | Force booklet page reordering |
| `copies` | no | Number of copies, 1-20 |
| `orientation` | no | `portrait` or `landscape`; booklet jobs force `landscape` |
| `media` | no | IPP media keyword, such as `iso_a4_210x297mm` |
| `raster_dpi` | no | Direct IPP raster conversion DPI, 72-600. Lower is faster; default is `150`. |

### `print_bridge.clear_queue`

Delete all `.pdf` files from the configured queue folder.

### `print_bridge.process_imap_part`

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
| `attachment_filter` | no | Only print if the filename contains this text |
| `copies` | no | Number of copies, 1-20 |
| `orientation` | no | `portrait` or `landscape`; booklet jobs force `landscape` |
| `media` | no | IPP media keyword, such as `iso_a4_210x297mm` |
| `raster_dpi` | no | Direct IPP raster conversion DPI, 72-600. Lower is faster; default is `150`. |
| `sender` | no | Original email sender, used for status replies |
| `mail_subject` | no | Original email subject, used for mail parameters and reply title |
| `mail_text` | no | Original plain-text email body, used for mail parameters |

### `print_bridge.process_imap_message`

Fetch all supported attachments from an IMAP message, convert them to PDF, merge
them in email attachment order, and submit one print job. This is the default
path used by the bundled blueprint because it is faster when one email has
multiple attachments.

| Field | Required | Description |
|---|---|---|
| `entry_id` | yes | IMAP config entry ID (`trigger.event.data.entry_id`) |
| `uid` | yes | Email UID (`trigger.event.data.uid`) |
| `parts` | no | Parts dictionary from `trigger.event.data.parts`; avoids one metadata fetch |
| `duplex` | no | Override duplex for this job |
| `booklet` | no | Force booklet page reordering |
| `attachment_filter` | no | Only include attachment filenames containing this text |
| `copies` | no | Number of copies, 1-20 |
| `orientation` | no | `portrait` or `landscape`; booklet imposition is landscape |
| `media` | no | IPP media keyword, such as `iso_a4_210x297mm` |
| `raster_dpi` | no | Direct IPP raster conversion DPI, 72-600. Lower is faster; default is `150`. |
| `sender` | no | Original email sender, used for status replies |
| `mail_subject` | no | Original email subject, used for mail parameters and reply title |
| `mail_text` | no | Original plain-text email body, used for mail parameters |

### `print_bridge.print_email`

Print all supported attachments from a specific email in your mailbox as one
combined job — on demand, without waiting for the email to arrive again. Get the
UID from the Lovelace email table or the `filter_preview` sensor.

| Field | Required | Description |
|---|---|---|
| `uid` | yes | IMAP UID of the email (shown in the Lovelace email table) |
| `imap_entry_id` | no | IMAP config entry ID — defaults to the first configured account |
| `duplex` | no | Override duplex for this job |
| `booklet` | no | Force booklet page reordering |
| `attachment_filter` | no | Only print matching attachment filenames |
| `copies` | no | Number of copies, 1-20 |
| `orientation` | no | `portrait` or `landscape`; booklet jobs force `landscape` |
| `media` | no | IPP media keyword, such as `iso_a4_210x297mm` |
| `raster_dpi` | no | Direct IPP raster conversion DPI, 72-600. Lower is faster; default is `150`. |

### Mail print parameters

You can include per-email print settings in the subject or body. These override the integration defaults and blueprint/service values for that email.

Subject form:

```text
[pb booklet=true duplex=short-edge copies=2 paper=a4 dpi=150 reply=true]
```

Body form:

```text
Print-Bridge: attachment="Au Puits"; orientation=landscape; paper=a4; quality=fast
```

Supported parameters:

| Parameter | Values |
|---|---|
| `duplex` / `sides` | `one-sided`, `simplex`, `long-edge`, `short-edge`, `two-sided-long-edge`, `two-sided-short-edge` |
| `booklet` | `true` / `false` |
| `copies` | `1` through `20` |
| `orientation` | `portrait` / `landscape`; booklet jobs always request landscape |
| `paper` / `media` | `a4`, `letter`, `legal`, or a raw IPP media keyword |
| `dpi` / `raster_dpi` | `72` through `600`; only used when direct IPP requires raster conversion |
| `quality` | `draft`, `fast`, `normal`, `high`, or `best` (`fast` maps to 150 DPI) |
| `attachment` / `attachment_filter` / `file` | Filename substring to print only matching attachments |
| `reply` / `status_reply` | `true` to request a status reply, `false` to suppress one |

Status replies include the IPP status code plus the effective printer settings used for each job. Print Bridge uses the configured Home Assistant notify service when set (for example `notify.smtp`); otherwise it attempts SMTP delivery through the matching HA IMAP account.

**From Developer Tools → Services:**
```yaml
service: print_bridge.print_email
data:
  uid: "42"
  duplex: two-sided-long-edge
```

**From an automation, after `check_filter`:**
```yaml
action: print_bridge.print_email
data:
  uid: "{{ state_attr('sensor.print_bridge_PRINTER_NAME_filter_preview', 'emails')[0].uid }}"
```

### `print_bridge.check_filter`

Connect to IMAP and list emails that match the current filter settings.
Returns a service response and updates `sensor.print_bridge_*_filter_preview`.

| Field | Required | Description |
|---|---|---|
| `imap_entry_id` | no | Which IMAP entry to query (defaults to first configured) |

---

## Audit Log

Every print attempt fires an `print_bridge_job_completed` event to the HA event bus.
This event appears in the native HA **Logbook** as a human-readable sentence:

> **Print Bridge** — Printed `invoice.pdf`  ·  two-sided-long-edge  ·  from billing@example.com  ·  Canon_MG3600_series

> **Print Bridge** — Print failed for `bad.pdf`: HTTP 503  ·  from sender@example.com

The `sensor.print_bridge_*_job_log` entity stores the last 50 jobs as attributes,
including timestamp, filename, success/failure, sender, duplex mode, and booklet flag.
It also stores the effective IPP sides, document format, status code, copies,
orientation, media, and raster DPI when available.

---

## How it works

```
Mail Server ──IMAP IDLE──► HA IMAP Integration ──imap_content event──► Print Bridge
                                                                              │
                          1. Check: is sender in allowed_senders?             │
                          2. Check: is folder in folder_filter?               │
                          3. Check: schedule day/hour/template open?          │
                          4. For each PDF part → imap.fetch_part ◄────────────┘
                          5. Mail parameters? → override duplex/booklet/copies/media/dpi
                          6. Booklet? → reorder pages + request landscape orientation
                          7. Build IPP/2.0 packet → POST to CUPS/printer
                          8. Fire print_bridge_job_completed → Logbook/status reply
```

---

## Blueprint — Advanced Per-Email Rules

For fine-grained per-sender or per-keyword rules (different duplex per sender,
booklet for some subjects, multiple printers), use the included automation blueprint.

### Import

**Settings → Automations → Blueprints → Import Blueprint** — paste:

```
https://github.com/rubeecube/ha-print-bridge/blob/main/blueprints/automation/print_bridge/print_from_email.yaml
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
        ├─ any printable attachment? ───────────────── no ──► skip
        │
        └─ convert matching attachments to PDF
                 apply mail parameters / blueprint settings
                 merge attachments into one job
                 apply booklet imposition after merge
                 ──► print_bridge.process_imap_message(duplex, booklet)
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

A paste-ready dashboard view is included at [`lovelace/printer_dashboard_template.yaml`](lovelace/printer_dashboard_template.yaml).
The fuller audit dashboard is still available at [`lovelace/print_bridge_audit.yaml`](lovelace/print_bridge_audit.yaml).

Add it as a new view to any dashboard (**Edit → Add View → Manual Configuration (YAML)**).
Replace `PRINTER_NAME` with the slug for your printer queue name.

The view includes:

- **Status row** — printer online, queue depth, jobs sent, last job status
- **Scheduled Queue table** — up to 5 waiting jobs with filename, sender, and queue time
- **Logbook card** — 7-day print event history
- **Recent jobs table** — last 50 jobs: timestamp / filename / ✅❌ / sender / duplex / booklet
- **Filter Preview table** — folder / subject / sender / ✅match / PDF count
- **Statistics graph** — daily job counts (30 days)
- **Action buttons** — Check Filter, Print Test Page, Clear Queue, Cancel Queued Jobs

---

## Developer Setup

```bash
git clone https://github.com/rubeecube/ha-print-bridge
cd ha-print-bridge
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

**Q: Do I need to configure IMAP credentials in Print Bridge?**  
A: No. Credentials live in HA's IMAP integration only. Print Bridge subscribes to the events it fires.

**Q: Can I print from multiple email addresses?**  
A: Yes — add one address per line in **Allowed Senders**, or leave it empty to accept all.

**Q: Can I filter by mailbox folder?**  
A: Yes — add one folder name per line in **IMAP Folder Filter** (e.g. `INBOX/Print`). The hint in the Options form shows the exact folder names your IMAP integration monitors. Leave empty to accept all folders.

**Q: How do I check which emails would be printed right now?**  
A: Press the **Check Filter** button (or call `print_bridge.check_filter`). Results appear in the `sensor.print_bridge_*_filter_preview` entity and in the Lovelace audit view.

**Q: What if my PDF is very large?**  
A: Print Bridge calls `imap.fetch_part` which has no size limit (unlike the 32 KB event body limit).

**Q: How do I enable booklet printing?**  
A: Add a substring of the PDF filename to **Booklet Patterns** in the options (e.g. `Programme`). Any attachment whose filename contains that string is automatically reordered for saddle-stitch printing.

**Q: What happens to the email after it's printed?**  
A: Configure **Email Action after Printing** in Options: **Mark as read** keeps it in your inbox but removes the unread badge; **Move to archive folder** moves it to `INBOX/Printed` (or a folder you choose) and marks it read; **Delete from server** removes it permanently. Default is **Do nothing** (no change to the email).

**Q: How do I get notified when a print job fails?**  
A: **Notify when print fails** is enabled by default. A HA persistent notification will appear in the bell (🔔) menu with the filename and error. Enable **Notify when print succeeds** if you also want confirmation of successful prints.

**Q: Do I need to install HA's built-in IPP integration?**  
A: No. HA's `ipp` integration ([docs](https://www.home-assistant.io/integrations/ipp/)) is a *printer monitor* — it reads ink levels, paper status, and error codes. Print Bridge is its own IPP client: it builds raw IPP/2.0 `Print-Job` packets and sends them directly to the printer or CUPS server via HTTP POST. The two integrations are independent and can be installed side-by-side.

| | HA IPP integration | Print Bridge |
|---|---|---|
| **Purpose** | Monitor printer state (ink, errors) | Send print jobs from email |
| **Protocol** | Reads IPP attributes | Writes IPP Print-Job |
| **Required for printing** | No | Yes |
| **Useful together** | Yes — monitors the same printer | Yes — sends jobs to same printer |

**Q: Do I need CUPS at all?**  
A: Not for modern AirPrint printers. If your printer has an IPP endpoint (most WiFi printers made after ~2015 do), use **Direct IPP mode** with a URL like `http://printer.local/ipp/print`. CUPS is useful when the printer is USB-attached, needs format conversion (PCL/PostScript), or you want a managed queue.

**Q: How do I find my printer's IPP URL?**  
A: Common paths: `http://printer.local/ipp/print`, `http://printer.local:631/ipp/print`, or the same paths with your printer host name. Your router's device list shows the printer's network name. For Canon PIXMA: the embedded server is usually at port 80 with path `/ipp/print`.

**Q: CUPS is on a different host. What URL do I use?**  
A: `http://<cups-host>:631`. When using the CUPS add-on on HA OS with host networking, the host can be your HA host name.

---

## Contributing

Pull requests are welcome. Please open an issue first for significant changes.

```bash
./venv/bin/pytest tests/ -v   # 146 tests, no external dependencies required
```

---

## License

[MIT](LICENSE) © 2026 rubeecube
