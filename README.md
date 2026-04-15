# modem-logger

A small tkinter GUI for logging, monitoring, and per-PLMN cell scanning with a **Quectel EG915U** LTE Cat 1 bis modem over a USB serial AT port.

## Features

- **Auto device discovery** — enumerates Quectel USB serial ports on startup with a Rescan button.
- **Live signal monitoring** — polls `AT+CSQ` every 5 s, renders as dBm, bars, and BER.
- **Full Network Scan** — single button (slow, 3–8 min) that enumerates every visible PLMN via `AT+COPS=?` and then captures rich per-cell data. Behaviour depends on SIM state:
  - **SIM present**: force-registers to each non-Forbidden PLMN in turn (`AT+COPS=1,2,…`), waits for `CEREG` to confirm registration, then records `CSQ` / `QCSQ` / `QENG="servingcell"`. Restores `AT+COPS=0` automatically in a `finally:` block. Successful PLMNs show full cell detail; rejected ones are flagged.
  - **No SIM**: skips registration (impossible without credentials) but uses limited-service (LIMSRV) camping plus `AT+QCELLINFO?` to capture serving + intra-frequency neighbour cells. The matching PLMN row is enriched with rich data; other rows show discovery info only.
- **Per-PLMN result table** with columns: Operator, MCC/MNC, RAT, PCI, EARFCN, Cell ID, TAC, RSRP, RSRQ, RSSI, SINR. Hover any column header for an explanation of the acronym and typical signal-quality ranges.
- **Console** — every raw AT command and response is timestamped, written to the UI log, and appended to `modem_log.txt`.
- **Versioned builds** — the window title shows `r<commit-count>+<short-sha>` so you can tell builds apart at a glance.

## Hardware

- Quectel EG915U (or any module that shares its AT command set — EC200U family).
- USB cable to a Windows host.

## Quick start (pre-built .exe)

If you just want to run the tool, grab `modem-logger.exe` from the [latest release](../../releases/latest). It's a single-file PyInstaller bundle — no Python or `uv` install needed. You still need the [Quectel USB driver](#3-install-the-quectel-usb-driver) (see below) for the modem to enumerate as a COM port.

## Windows setup (from source)

### 1. Install Python 3.11+

Recommended: install via [python.org](https://www.python.org/downloads/windows/) and tick **"Add python.exe to PATH"** in the installer.

### 2. Install uv

`uv` manages the project's virtual environment and dependencies. In PowerShell:

```powershell
winget install --id=astral-sh.uv -e
```

or via the standalone installer:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

Verify: `uv --version`.

### 3. Install the Quectel USB driver

Windows won't enumerate the EG915U's AT port without the vendor driver. Download **"Quectel LTE&5G Windows USB Driver"** from the [Quectel download center](https://www.quectel.com/download/) (or from Quectel support if you have access) and run the installer. After a reboot and plugging the modem in, Device Manager should show several `Quectel USB ...` COM ports under **Ports (COM & LPT)**.

### 4. Clone and sync the project

```powershell
git clone <this repo>
cd modem_logger
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock` and creates a local `.venv` with the correct `pyserial` version.

### 5. Run the app

```powershell
uv run python main.py
```

The app opens, auto-detects Quectel ports, and selects the first one. If nothing appears in the device dropdown, check Device Manager — you usually want the port labeled **"Quectel USB AT Port"**, not the DM or Modem port.

## Notes

- **A Full Network Scan takes a while.** `AT+COPS=?` alone can take up to 3 minutes; with a SIM present, each per-PLMN registration adds another 20–35 s. Plan for 3–8 minutes total. The status label updates per PLMN so you can watch progress.
- **Without a SIM the modem still camps in LIMSRV mode** on the strongest cell it can find. That's where the rich data comes from in no-SIM scans — `QENG="servingcell"` reports state `LIMSRV` plus full cell detail, and `QCELLINFO?` adds intra-frequency neighbour cells (marked `(nbr)` in the Operator column). Cross-PLMN data without a SIM is fundamentally limited to whichever cell the modem decides to camp on.
- **Forbidden PLMNs are skipped.** Rows where `COPS=?` reports `stat=3` are dropped before the registration loop — they would always reject with `+CME ERROR: 30` and waste time.
- **GSM rows give less detail than LTE.** `QCSQ` and `QENG="servingcell"` are LTE-focused; GSM-only PLMNs show CSQ-derived dBm in the RSRP column and dashes elsewhere.
- **Only one thing can hold the serial port at a time.** Close any other tool (QNavigator, PuTTY, etc.) before starting the app.
- **Signal polling and scans are mutually exclusive.** Stop polling before launching a Full Network Scan.
- **Data connectivity drops during a scan.** Manual registration to each PLMN tears down whatever data session was active. The trailing `AT+COPS=0` restores automatic mode at the end.
- **EG915U is LTE + GSM only** (Cat 1 bis). No UMTS/HSPA. Expect `AcT = 0 (GSM)` or `7 (LTE)` in results.

## Adding a dependency

```powershell
uv pip install <package>
```

Then add the package (with a version constraint) to `dependencies` in `pyproject.toml` so `uv sync` picks it up on other machines, and commit both files together with `uv.lock`.
