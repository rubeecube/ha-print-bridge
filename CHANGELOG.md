# Changelog

All notable changes to **Print Bridge** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.1.3] — 2026-04-23

### Fixed

- **`KeyError: 'printer_name'`** — all entities (sensor, binary\_sensor, button) crashed with this error when using Direct IPP mode (no CUPS). The `_device_info()` helper now uses `.get()` with a hostname fallback so it works in both CUPS and Direct IPP configurations.
- **Blocking I/O on HA event loop** — `os.listdir()`, `os.remove()`, and `open()` calls in `coordinator.py` were running synchronously on the async event loop, triggering HA's loop-blocking detector. All three callsites are now wrapped in `hass.async_add_executor_job()`.
- **mDNS discovery returning non-IPP URLs** — `_printer._tcp.local.` (LPD/LPR protocol, port 515) was incorrectly included in the IPP service type list. Removed. The path `ipp/auto` (a CUPS virtual queue) is now skipped. `printers/*` CUPS queue paths are replaced with the standard `ipp/print` fallback.
- **SSL certificate error for IPPS printers** — `_ipps._tcp.local.` services advertise HTTPS/port 443, but home printers use self-signed certificates that fail HA's default SSL verification. Discovery now generates an HTTP fallback URL, and the validation HEAD request uses `verify_ssl=False`.

### Added

- **Auto-print toggle** (`auto_print_enabled` option, default `False` on first install). Choose between:
  - **Enable auto-print** — simple mode; the integration automatically prints PDFs from matching senders/folders.
  - **Use Blueprint** — advanced mode; disable auto-print and drive printing from the automation blueprint with per-sender/per-keyword rules.
- **Blueprint auto-install** — the automation blueprint is now bundled inside the component (`blueprints/automation/print_bridge/`). `async_setup()` copies it to `/config/blueprints/` automatically on HA startup; no manual import required.
- **Persistent notification on first install** — explains the two printing modes and links to the blueprint import and the dashboard YAML.
- **Redesigned Lovelace dashboard** (`lovelace/print_bridge_audit.yaml`):
  - Status row uses Jinja2 auto-detection — no `PRINTER_NAME` slug needed for status, email list, or job history.
  - 4-button action grid: Scan Mailbox, Print Test Page, Retry Last Failed, Print Queued Jobs.
  - One-click **Install Blueprint** button (uses My Home Assistant redirect URL).
  - Recent jobs and mailbox email list auto-detect entities.
  - Management links: Settings, Logbook, Clear Queue.

---

## [0.1.2] — 2026-04-23

### Added

- **`print_bridge.print_email` service** — print all PDF attachments from any email in the mailbox by IMAP UID. Calls `imap.fetch` to find parts, then `process_imap_part` for each PDF. Returns `{uid, printed, results[]}` as a service response.
- **Logo displayed in README** — `hacs.png` shown at the top via raw GitHub URL.
- **Platform compatibility table** in README — HA OS, Supervised, Container (host network), and Core all supported; Docker bridge network has limited mDNS discovery.
- **Lovelace email table** shows email UIDs with a `print_email(uid=...)` call snippet for each PDF.
- Transparent-background versions of `icon.png` and `hacs.png` — adapts to HA light/dark theme.

---

## [0.1.1] — 2026-04-23

### Fixed

- **mDNS printer discovery** — the original `AsyncServiceBrowser` had event-loop scheduling conflicts with `HaZeroconf`. Replaced with a synchronous `ServiceBrowser` running in `hass.async_add_executor_job`. Service info is now resolved inside the `add_service` callback while the browser is live.
- **CUPS fields required even in Direct IPP mode** — `CONF_CUPS_URL` and `CONF_PRINTER_NAME` changed from `vol.Required` to `vol.Optional`. A validation error is shown when neither a Direct IPP URL nor a CUPS URL+name is provided.
- **Hardcoded IP address** — `DEFAULT_CUPS_URL` was `"http://10.0.0.23:631"`. Changed to `""` (filled from discovery or typed by the user).
- **CUPS fields shown when CUPS is not installed** — the setup form now conditionally shows CUPS fields only when CUPS was discovered on the HA host. When not found, an explanatory message is shown instead.

### Added

- **LAN printer discovery via mDNS** — uses HA's Zeroconf instance to browse `_ipp._tcp.local.` and `_ipps._tcp.local.` services. Finds AirPrint printers on the local network from the HA host (server-side, not the browser).
- **"Scan again" checkbox** — re-runs discovery without leaving the setup form.
- **Discovery timeout** increased to 5 seconds for slow mDNS stacks.
- **Printer-on guidance** — when no printers are found, the form shows a checklist: turn printer on, wait 30 s, rescan, or type the IPP URL manually.
- `zeroconf` added to `after_dependencies` in `manifest.json`.

---

## [0.1.0] — 2026-04-23

### Initial release

#### Core features

- **Event-driven printing** — subscribes to `imap_content` events from HA's built-in IMAP integration; no separate IMAP connection or credentials stored.
- **Direct IPP mode** — sends IPP/2.0 `Print-Job` packets directly to AirPrint printers (no CUPS required for modern WiFi printers).
- **CUPS mode** — prints via a CUPS server (local add-on or remote host).
- **Smart setup wizard** — auto-discovers CUPS on localhost and pre-fills sender from existing IMAP config entries.
- **Sender filter** — accept only specific email addresses (or all).
- **IMAP folder filter** — accept only emails from specific folders (e.g. `INBOX/Print`).
- **Duplex control** — one-sided, two-sided long-edge (portrait), two-sided short-edge (landscape).
- **Booklet printing** — automatic saddle-stitch page reordering for filenames matching configurable patterns.
- **Email post-processing** — mark as read, move to archive folder, or delete after printing.
- **Print schedule** — allowed time window with deferred queue; jobs outside the window are held and flushed automatically when the window opens.
- **Retry** — re-fetch and reprint any email job from history using its stored IMAP metadata.
- **On-demand print** — `print_bridge.print_email` service to print any mailbox email by UID.

#### Entities

- `sensor.*_print_queue_depth` — PDF files in queue folder.
- `sensor.*_last_print_job` — last job status with sender, duplex, booklet, timestamp.
- `sensor.*_job_log` — cumulative count + last 50 jobs with full metadata.
- `sensor.*_filter_preview` — mailbox scan results (email list with UIDs).
- `sensor.*_scheduled_queue` — jobs held by the print schedule.
- `binary_sensor.*_printer_online` — CUPS/printer reachability.
- `button.*_print_test_page`, `*_check_filter`, `*_retry_last_failed_job`, `*_print_queued_jobs_now`, `*_check_filter`.

#### Services

`print_file`, `clear_queue`, `process_imap_part`, `check_filter`, `retry_job`, `print_email`.

#### Audit

- `print_bridge_job_completed` event fires after every print attempt → appears in HA Logbook.
- Custom Logbook descriptor formats events as human-readable sentences.
- `pyproject.toml` with full metadata for GitHub dependency graph.
- `hacs.json` with `render_readme: true`.
- Git hooks (`.githooks/`) block commits/pushes from non-canonical author identities.

---

[0.1.3]: https://github.com/rubeecube/ha-print-bridge/releases/tag/v0.1.3
[0.1.2]: https://github.com/rubeecube/ha-print-bridge/releases/tag/v0.1.2
[0.1.1]: https://github.com/rubeecube/ha-print-bridge/releases/tag/v0.1.1
[0.1.0]: https://github.com/rubeecube/ha-print-bridge/releases/tag/v0.1.0
