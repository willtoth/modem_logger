# modem-logger

A small tkinter GUI for logging, monitoring, and site-scanning with a **Quectel EG915U** LTE Cat 1 bis modem over a USB serial AT port.

## Features

- **Auto device discovery** — enumerates Quectel USB serial ports on startup with a Rescan button.
- **Live signal monitoring** — polls `AT+CSQ` every 5 s, renders as dBm, bars, and BER.
- **Site scan** — runs `AT+CEREG?` / `AT+QNWINFO` / `AT+QCSQ` / `AT+QENG="servingcell"` / `AT+QCELLINFO?` / `AT+QENG="neighbourcell"` in sequence (~3–8 s) and displays the serving cell plus every neighbor cell the modem has measured, with MCC/MNC, PCI, EARFCN, cell ID, TAC, RSRP, RSRQ, and SINR.
- **PLMN scan** — on-demand `AT+COPS=?` (slow, 1–3 min) to enumerate every visible carrier.
- **Console** — every raw AT command and response is timestamped, written to the UI log, and appended to `modem_log.txt`.

## Hardware

- Quectel EG915U (or any module that shares its AT command set — EC200U family).
- USB cable to a Windows host.

## Windows setup

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

- **Cold-start site scans are empty.** The EG915U can only report cells its RRC layer has measured. Until the modem has attached to the network (Signal polling shows a usable RSSI, or CEREG shows `home`/`roaming`), a site scan typically returns just the serving row. Give it ~10 seconds after power-on.
- **Only one thing can hold the serial port at a time.** Close any other tool (QNavigator, PuTTY, etc.) before starting the app.
- **Signal polling and scans are mutually exclusive.** Stop polling before launching a site or PLMN scan.
- **EG915U is LTE + GSM only** (Cat 1 bis). No UMTS/HSPA. Expect `AcT = 0 (GSM)` or `7 (LTE)` in results.

## Adding a dependency

```powershell
uv pip install <package>
```

Then add the package (with a version constraint) to `dependencies` in `pyproject.toml` so `uv sync` picks it up on other machines, and commit both files together with `uv.lock`.
