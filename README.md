# Auto Print

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Print PDF attachments from any email address directly to a network printer — without leaving Home Assistant.

**Auto Print** bridges HA's built-in IMAP integration with a CUPS print server.  
When an email with a PDF attachment arrives from an allowed sender, the component fetches the attachment via `imap.fetch_part`, optionally reorders pages for booklet printing, and sends an IPP/2.0 Print-Job directly to CUPS.

---

## Features

- **Event-driven** — no polling; triggered instantly by HA's IMAP integration push (IDLE)
- **Booklet printing** — automatic saddle-stitch page reordering for any filename matching a configured pattern
- **Duplex control** — one-sided, long-edge (portrait), or short-edge (landscape) per job or globally
- **Print queue sensor** — count of PDF files waiting in the queue folder
- **Printer online sensor** — binary sensor tracking CUPS reachability
- **Test page button** — one-click print without needing an email
- **Services** — `auto_print.print_file` and `auto_print.clear_queue` for automations

---

## Prerequisites

### 1. HA IMAP Integration (built-in)

The IMAP integration must be set up and pointed at the mailbox that receives print jobs.

> Settings → Devices & Services → Add Integration → **IMAP**

Configure it with your mail server details.  The Auto Print component listens to the `imap_content` events it fires — no credentials are stored here.

### 2. CUPS Print Server

A CUPS instance reachable from Home Assistant is required.  The recommended option for HA OS users is the **[CUPS add-on by peternicholls](https://github.com/peternicholls/ha-cups-addon)**.

#### Using the CUPS add-on

1. In HA, go to **Settings → Add-ons → Add-on Store**.
2. Add the repository: `https://github.com/peternicholls/ha-cups-addon`
3. Install **CUPS Print Server** and start it.
4. Open the CUPS web UI at `http://<ha-host>:631` and add your printer.
5. Note the printer's **queue name** (shown in the CUPS Printers list).

---

## Installation

### HACS (recommended)

1. In HA, open **HACS → Integrations**.
2. Click the three-dot menu (⋮) → **Custom repositories**.
3. Add `https://github.com/rubeecube/custom_component_hassio_print` with category **Integration**.
4. Search for **Auto Print** and install it.
5. Restart Home Assistant.

### Manual

1. Copy `custom_components/auto_print/` into your HA config directory:
   ```
   /config/custom_components/auto_print/
   ```
2. Restart Home Assistant.

---

## Configuration

### Step 1 — Set up HA's IMAP integration

Before adding Auto Print, configure the IMAP integration (see [Prerequisites](#1-ha-imap-integration-built-in)) so it polls the mailbox you want to print from.

### Step 2 — Add the Auto Print integration

> Settings → Devices & Services → Add Integration → **Auto Print**

| Field | Example | Description |
|---|---|---|
| CUPS Base URL | `http://10.0.0.23:631` | Root URL of your CUPS server |
| Printer Name | `Canon_MG3600_series` | CUPS queue name, as shown in the Printers list |

### Options (editable after setup)

> Settings → Devices & Services → Auto Print → **Configure**

| Option | Default | Description |
|---|---|---|
| Allowed Senders | *(empty)* | One email address per line. Empty = accept all. |
| Default Duplex Mode | Two-sided (portrait) | Applied to all jobs unless overridden per service call. |
| Booklet Patterns | *(empty)* | Filename substrings that trigger booklet page reordering. |
| Delete after printing | On | Remove the file from the queue folder after a successful print. |
| Print Queue Folder | `/media/print_queue` | Used by the `print_file` service and the queue-depth sensor. |

---

## Entities

| Entity | Type | Description |
|---|---|---|
| `sensor.auto_print_queue_depth` | Sensor | Number of PDF files in the queue folder |
| `sensor.auto_print_last_job` | Sensor | `success` or `failed` for the most recent job; `last_filename` and `last_status` attributes |
| `binary_sensor.auto_print_printer_online` | Binary Sensor | `on` when CUPS responds; `off` when unreachable |
| `button.auto_print_print_test_page` | Button | Sends a built-in single-page PDF to the printer |

---

## Services

### `auto_print.print_file`

Print a PDF already on the HA filesystem (e.g. `/media/print_queue/doc.pdf`).

| Field | Required | Description |
|---|---|---|
| `file_path` | yes | Absolute path to the PDF |
| `duplex` | no | Override duplex mode for this job |
| `booklet` | no | Force booklet page reordering |

### `auto_print.clear_queue`

Delete all `.pdf` files from the configured queue folder.

---

## How it works

```
Mail Server ──IMAP IDLE──► HA IMAP Integration ──imap_content event──► Auto Print
                                                                              │
                                            ◄── imap.fetch_part (bytes) ──────┘
                                                                              │
                                                          IPP Print-Job ──► CUPS ──► Printer
```

1. HA's IMAP integration detects a new email and fires an `imap_content` event.
2. Auto Print checks the sender against **Allowed Senders** (if configured).
3. For each PDF part in `event.data["parts"]`, it calls `imap.fetch_part` to retrieve the raw bytes.
4. If the filename matches a **Booklet Pattern**, pages are reordered for saddle-stitch printing.
5. An IPP/2.0 `Print-Job` packet is sent to CUPS. The `sensor.auto_print_last_job` is updated.

---

## FAQ

**Q: Do I need to configure IMAP credentials in Auto Print?**  
A: No. Credentials live in HA's IMAP integration only. Auto Print listens to the events it fires.

**Q: Can I print from multiple email addresses?**  
A: Yes — add one address per line in **Allowed Senders**, or leave it empty to accept all.

**Q: What if my PDF is very large?**  
A: Auto Print calls `imap.fetch_part` which has no size limit (unlike the 32 KB event body limit). Large PDFs are handled correctly.

**Q: How do I enable booklet printing for "Sunday Programme" booklets?**  
A: Add `Programme` to **Booklet Patterns** in the options. Any email attachment whose filename contains that substring will be automatically reordered and printed two-sided on the short edge.

**Q: CUPS is on a different host from HA. What URL do I use?**  
A: Use `http://<cups-host-ip>:631`. If using the CUPS add-on on HA OS with host networking, the host IP is typically your HA host's LAN IP.

---

## Blueprint — Advanced Email Rules

For fine-grained control (multiple senders, per-keyword duplex/booklet rules, multiple printers), use the included automation blueprint instead of the built-in event listener.

### Import the blueprint

In Home Assistant go to **Settings → Automations → Blueprints → Import Blueprint** and paste:

```
https://github.com/rubeecube/custom_component_hassio_print/blob/main/blueprints/automation/auto_print/print_from_email.yaml
```

### Blueprint inputs

| Input | Description |
|---|---|
| IMAP Account | Select the HA IMAP integration entry to monitor |
| Allowed Senders | Comma-separated addresses; empty = accept all |
| Subject Must Contain | Optional keyword gate on the subject line |
| Default Duplex Mode | Fallback for all jobs |
| One-sided Keywords | Subject keywords that override to one-sided |
| Two-sided Portrait Keywords | Subject keywords that force long-edge duplex |
| Booklet Keywords | Subject keywords that trigger booklet page reordering |
| Booklet Senders | Senders whose mail is always printed as a booklet |
| Mark as Seen | Whether to set the \\Seen flag after processing |

### Decision logic

```
imap_content event received
        │
        ├─ sender in allowed_senders? (empty = yes) ──── no ──► skip
        │
        ├─ subject contains filter? (empty = yes) ──────── no ──► skip
        │
        ├─ any PDF part? ─────────────────────────────── no ──► skip
        │
        └─ for each PDF part:
                 is_booklet = subject has booklet keyword OR sender in booklet_senders
                 duplex    = "two-sided-short-edge"  if is_booklet
                           = "one-sided"             if subject has one-sided keyword
                           = "two-sided-long-edge"   if subject has two-sided keyword
                           = default_duplex          otherwise
                 ──► auto_print.process_imap_part(duplex, booklet)
                 ──► imap.seen  (if mark_as_seen)
```

### Example: Separate rules for invoices vs. programmes

Create **two** automations from the blueprint:

**Automation 1 — Invoices (one-sided):**
- Allowed Senders: `billing@mybank.com`
- One-sided Keywords: `INVOICE`
- Default Duplex: One-sided

**Automation 2 — Church programmes (booklet):**
- Allowed Senders: `liturgie@church.org`
- Booklet Keywords: `Programme, Bulletin`
- Booklet Senders: `liturgie@church.org`

The `auto_print.process_imap_part` service used by the blueprint also works directly in any automation or script.

---

## Contributing

Pull requests are welcome. Please open an issue first for significant changes.

```bash
# Run the test suite
./venv/bin/pytest tests/ -v
```

---

## License

[MIT](LICENSE) © 2026 rubeecube
