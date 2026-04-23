# Legacy Shell-Command Approach

> **This directory contains the original shell-command + Python script pipeline.**
> For new installations, use the **[Print Bridge custom component](../README.md)** instead —
> it is self-contained, configurable from the HA UI, and requires no manual file deployment.

---

## What is here

| File | Purpose |
|---|---|
| `shell_commands.yaml` | HA `shell_command:` entries for downloading and printing |
| `automations.yaml` | Two HA automations wiring IMAP events to the shell commands |
| `pdf_downloader.py` | Downloads a PDF attachment directly via `imaplib` (bypasses HA's 256 KB event limit) |
| `print_handler.py` | Sends a PDF to CUPS via a raw IPP/2.0 HTTP request |
| `booklet_maker.py` | Reorders pages for saddle-stitch booklet printing |
| `shell_commands_canon_printer.yaml` | Diagnostic shell commands for checking printer connectivity |

---

## Why use the custom component instead

| | Shell commands | Custom component |
|---|---|---|
| Config | Manual YAML + secrets | HA UI config flow |
| IMAP credentials | In `secrets.yaml` on disk | Stored in HA credential store |
| CUPS URL | Hardcoded env var | Configurable per integration |
| Booklet patterns | Hardcoded string in Python | UI options, editable any time |
| Duplex control | Hardcoded in automation | Per-job or global option |
| Entities / sensors | None | Queue depth, last job, printer online |
| Testing | Manual | 82-test suite (unit + HA integration) |

---

## If you still want to use the shell-command approach

### 1. Deploy scripts

Copy the Python scripts to your HA config directory:

```bash
cp pdf_downloader.py print_handler.py booklet_maker.py /config/
```

Install dependencies in HA's Python environment (HAOS terminal):

```bash
pip3 install pypdf requests
```

### 2. Include shell commands

In `configuration.yaml`:

```yaml
shell_command: !include shell_commands.yaml
```

### 3. Set secrets

In `secrets.yaml`:

```yaml
email_user: print@example.com
email_password: your_imap_password
```

### 4. Include automations

In `configuration.yaml`:

```yaml
automation: !include automations.yaml
```

Or paste the contents of `automations.yaml` into your existing automation file.

### 5. Set environment variables

The print handler reads from environment variables (overridable at runtime):

| Variable | Default | Description |
|---|---|---|
| `AUTO_PRINT_PRINTER_NAME` | `Canon_MG3600_series` | CUPS queue name |
| `AUTO_PRINT_CUPS_URL` | `http://10.0.0.23:631` | CUPS base URL |
| `AUTO_PRINT_BOOKLET_MARKER` | `Programme` | Substring triggering booklet mode |
| `AUTO_PRINT_LOG_FILE` | *(none)* | Optional log file path |

---

## Known limitations (fixed in the custom component)

- `pdf_downloader.py` uses raw `imaplib` — credentials on disk.
- CUPS URL is not configurable via the HA UI.
- No entities, no sensors, no test-page button.
- No test coverage.
