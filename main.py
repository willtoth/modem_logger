import tkinter as tk
from tkinter import scrolledtext, ttk
import serial
from serial.tools import list_ports
import threading
import time
import datetime
import os
import re
import subprocess


def _detect_version():
    # Prefer a baked-in version (CI writes _version.py before PyInstaller
    # bundles the .exe). Fall back to live git for local dev. Last resort
    # is "dev".
    try:
        from _version import __version__ as baked
        return baked
    except ImportError:
        pass
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=here, stderr=subprocess.DEVNULL,
        ).decode().strip()
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return f"r{count}+{sha}"
    except Exception:
        return "dev"


__version__ = _detect_version()

BAUDRATE = 115200
# Quectel modules expose ~7 USB interfaces (Diag, AP Log, MOS, NMEA, AT, CP Log,
# Modem). Only the one labeled "AT Port" accepts AT commands — match on that
# exact substring to filter out the other six.
AT_PORT_SUBSTR = "at port"

CSQ_BER = {
    0: "< 0.2%",
    1: "0.2-0.4%",
    2: "0.4-0.8%",
    3: "0.8-1.6%",
    4: "1.6-3.2%",
    5: "3.2-6.4%",
    6: "6.4-12.8%",
    7: ">= 12.8%",
    99: "—",
}

# (label, treeview-tag)
COPS_STAT = {
    0: ("Unknown", ""),
    1: ("Available", ""),
    2: ("Current", "current"),
    3: ("Forbidden", "forbidden"),
}

# 3GPP 27.007 access technology values. EG915U only emits 0 and 7,
# but we keep the full table for defensive parsing.
COPS_ACT = {
    0: "GSM",
    1: "GSM Compact",
    2: "UTRAN",
    3: "GSM/EGPRS",
    4: "UTRAN/HSDPA",
    5: "UTRAN/HSUPA",
    6: "UTRAN/HSDPA+HSUPA",
    7: "LTE",
    8: "EC-GSM-IoT",
    9: "NB-IoT",
}

CEREG_STAT = {
    0: "not registered",
    1: "home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "roaming",
}

# Representative LTE downlink EARFCNs for the QNWLOCK no-SIM band sweep.
# Limited to the LTE bands the EG915U-EU actually supports (B1, B3, B7,
# B8, B20, B28). One probe per band at roughly band-center; the modem
# camps on whatever cell it can hear on that channel. Each candidate
# adds ~12 s to the scan.
LTE_SWEEP_EARFCNS = [
    (6300, "B20/800"),    # EU/Africa coverage band
    (9435, "B28/700"),    # APT 700 — Africa/SEA
    (1575, "B3/1800"),    # Near-universal urban LTE
    (300,  "B1/2100"),    # Europe/APAC capacity
    (3625, "B8/900"),     # Refarmed GSM band
    (3100, "B7/2600"),    # Urban capacity
]


def find_modem_ports():
    ports = []
    for p in list_ports.comports():
        if AT_PORT_SUBSTR in (p.description or '').lower():
            ports.append(p)
    return ports


def csq_rssi_to_dbm(rssi):
    if rssi == 99:
        return None
    if rssi == 0:
        return -113
    if rssi == 31:
        return -51
    if 1 <= rssi <= 30:
        return -113 + 2 * rssi
    return None


def csq_bars(dbm):
    if dbm is None:
        return 0
    if dbm >= -75:
        return 4
    if dbm >= -85:
        return 3
    if dbm >= -95:
        return 2
    if dbm >= -105:
        return 1
    return 0


def parse_csq(response):
    m = re.search(r'\+CSQ:\s*(\d+),(\d+)', response or '')
    if not m:
        return None
    rssi = int(m.group(1))
    ber = int(m.group(2))
    dbm = csq_rssi_to_dbm(rssi)
    return {
        "rssi": rssi,
        "ber": ber,
        "dbm": dbm,
        "bars": csq_bars(dbm),
        "ber_text": CSQ_BER.get(ber, f"ber={ber}"),
    }


def _extract_parens(s):
    out, depth, start = [], 0, -1
    for i, ch in enumerate(s):
        if ch == '(':
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(s[start:i])
                start = -1
    return out


def _split_csv_quoted(s):
    out, cur, in_quote = [], [], False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            out.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append(''.join(cur).strip())
    return out


def parse_cops(response):
    ops = []
    for tup in _extract_parens(response or ''):
        fields = _split_csv_quoted(tup)
        if len(fields) < 4:
            continue
        try:
            stat = int(fields[0])
        except ValueError:
            continue  # trailing (mode) / (format) ranges — skip
        long_name, short_name, numeric = fields[1], fields[2], fields[3]
        act = None
        if len(fields) >= 5 and fields[4]:
            try:
                act = int(fields[4])
            except ValueError:
                act = None
        name = long_name or short_name or numeric
        stat_label, stat_tag = COPS_STAT.get(stat, (f"?{stat}", ""))
        mccmnc = f"{numeric[:3]}/{numeric[3:]}" if len(numeric) >= 5 else numeric
        ops.append({
            "stat": stat,
            "stat_label": stat_label,
            "stat_tag": stat_tag,
            "name": name,
            "numeric": numeric,
            "mccmnc": mccmnc,
            "act": act,
            "act_label": COPS_ACT.get(act, "—") if act is not None else "—",
        })
    return ops


def parse_cme_error(response):
    m = re.search(r'\+CME ERROR:\s*(\d+)', response or '')
    return int(m.group(1)) if m else None


def dedupe_plmns(ops):
    # Drop Forbidden (stat=3) and entries missing numeric/act. Collapse
    # duplicate (numeric, act) pairs. Order so the original Current entry
    # (stat=2) sorts last — that way the final AT+COPS=0 re-registers to
    # the same operator the modem started on, minimizing churn.
    seen = set()
    out = []
    for op in ops:
        if op.get("stat") == 3 or not op.get("numeric") or op.get("act") is None:
            continue
        key = (op["numeric"], op["act"])
        if key in seen:
            continue
        seen.add(key)
        out.append(op)
    out.sort(key=lambda o: (o.get("stat") == 2, o["numeric"], o["act"]))
    return out


def _int_or_none(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _str_or_none(v):
    if v is None or v == '' or v == '-':
        return None
    return v


def qeng_sinr_to_db(raw):
    """Convert Quectel SINR field to dB. EG915U firmware may report
    either a raw encoded value (Y = X/2 - 23.5) or a pre-converted dB
    value. Heuristic: if it already fits the -25..35 dB range, trust it.
    Otherwise apply the conversion and clip to spec range."""
    if raw is None:
        return None
    if -25 <= raw <= 35:
        return float(raw)
    db = raw / 2.0 - 23.5
    return max(-25.0, min(35.0, db))


def parse_qnwinfo(response):
    if not response:
        return None
    if 'No Service' in response and ',' not in response:
        return {"act": "No Service", "oper": None, "band": None, "channel": None}
    m = re.search(r'\+QNWINFO:\s*"([^"]*)","([^"]*)","([^"]*)",(\d+)', response)
    if not m:
        return None
    return {
        "act": m.group(1),
        "oper": m.group(2),
        "band": m.group(3),
        "channel": int(m.group(4)),
    }


def parse_qcsq(response):
    m = re.search(
        r'\+QCSQ:\s*"([^"]+)"(?:,(-?\d+))?(?:,(-?\d+))?(?:,(-?\d+))?(?:,(-?\d+))?',
        response or '',
    )
    if not m:
        return None
    sysmode = m.group(1)
    vals = [int(x) if x is not None else None for x in m.groups()[1:]]
    result = {"sysmode": sysmode, "rssi": None, "rsrp": None, "sinr": None, "rsrq": None}
    if sysmode == "LTE":
        result["rssi"] = vals[0]
        result["rsrp"] = vals[1]
        result["sinr"] = qeng_sinr_to_db(vals[2])
        result["rsrq"] = vals[3]
    elif sysmode == "GSM":
        result["rssi"] = vals[0]
    return result


def parse_cereg(response):
    m = re.search(
        r'\+CEREG:\s*(\d+),(\d+)(?:,"([^"]*)","([^"]*)")?(?:,(\d+))?',
        response or '',
    )
    if not m:
        return None
    stat = int(m.group(2))
    return {
        "stat": stat,
        "stat_label": CEREG_STAT.get(stat, f"?{stat}"),
        "tac": m.group(3),
        "ci": m.group(4),
        "act": int(m.group(5)) if m.group(5) else None,
    }


def parse_qeng_servingcell(response):
    for line in (response or '').splitlines():
        m = re.match(r'\+QENG:\s*"servings?cell",(.*)', line)
        if not m:
            continue
        fields = _split_csv_quoted(m.group(1))
        if not fields:
            return None
        state = fields[0]
        if len(fields) < 2:
            return {"state": state, "rat": None}
        rat = fields[1]

        def _val(i):
            return fields[i] if i < len(fields) else None

        if rat == "LTE":
            return {
                "state": state,
                "rat": "LTE",
                "is_tdd": _str_or_none(_val(2)),
                "mcc": _int_or_none(_val(3)),
                "mnc": _int_or_none(_val(4)),
                "cell_id": _str_or_none(_val(5)),
                "pci": _int_or_none(_val(6)),
                "earfcn": _int_or_none(_val(7)),
                "band": _int_or_none(_val(8)),
                "ul_bw": _int_or_none(_val(9)),
                "dl_bw": _int_or_none(_val(10)),
                "tac": _str_or_none(_val(11)),
                "rsrp": _int_or_none(_val(12)),
                "rsrq": _int_or_none(_val(13)),
                "rssi": _int_or_none(_val(14)),
                "sinr": qeng_sinr_to_db(_int_or_none(_val(15))),
                "srxlev": _int_or_none(_val(16)),
            }
        if rat == "GSM":
            return {
                "state": state,
                "rat": "GSM",
                "mcc": _int_or_none(_val(2)),
                "mnc": _int_or_none(_val(3)),
                "lac": _str_or_none(_val(4)),
                "cell_id": _str_or_none(_val(5)),
                "bsic": _int_or_none(_val(6)),
                "arfcn": _int_or_none(_val(7)),
                "band": _str_or_none(_val(8)),
                "rx_lev": _int_or_none(_val(9)),
            }
        return {"state": state, "rat": rat}
    return None


def parse_qcellinfo(response):
    # QuecCell response: one line per cell (serving + decoded intra-freq
    # neighbours). Works in LIMSRV mode — no SIM required.
    cells = []
    for line in (response or '').splitlines():
        m = re.match(r'\+QCELLINFO:\s*"(servingcell|neighbourcell)","LTE",(.*)', line)
        if not m:
            continue
        kind = m.group(1)
        fields = _split_csv_quoted(m.group(2))

        def _val(i):
            return fields[i] if i < len(fields) else None

        cells.append({
            "kind": kind,
            "rat": "LTE",
            "mcc": _int_or_none(_val(0)),
            "mnc": _int_or_none(_val(1)),
            "tac": _str_or_none(_val(2)),
            "cell_id": _str_or_none(_val(3)),
            "pci": _int_or_none(_val(4)),
            "rx_lev": _int_or_none(_val(5)),
            "rx_dbm": _int_or_none(_val(6)),
            "earfcn": _int_or_none(_val(7)),
            "rsrp": _int_or_none(_val(8)),
            "rssi": _int_or_none(_val(9)),
            "sinr": qeng_sinr_to_db(_int_or_none(_val(10))),
        })
    return cells


class TreeHeaderTooltip:
    # Hover-tooltip for ttk.Treeview column headings. tkinter has no built-in
    # tooltip, so we manage a tiny Toplevel manually based on cursor region.
    def __init__(self, tree, descriptions):
        self._tree = tree
        self._descriptions = descriptions
        self._tip = None
        self._current = None
        tree.bind("<Motion>", self._on_motion)
        tree.bind("<Leave>", self._hide)

    def _on_motion(self, event):
        if self._tree.identify_region(event.x, event.y) != "heading":
            self._hide()
            return
        col_id = self._tree.identify_column(event.x)  # "#1", "#2", ...
        try:
            idx = int(col_id.lstrip("#")) - 1
            col_name = self._tree["columns"][idx]
        except (ValueError, IndexError):
            self._hide()
            return
        if col_name == self._current:
            return
        self._hide()
        desc = self._descriptions.get(col_name)
        if not desc:
            return
        self._current = col_name
        self._tip = tk.Toplevel(self._tree)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 18}")
        tk.Label(
            self._tip, text=desc,
            background="#ffffe0", relief="solid", borderwidth=1,
            wraplength=360, justify="left", padx=6, pady=4,
            font=("TkDefaultFont", 9),
        ).pack()

    def _hide(self, _event=None):
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        self._current = None


COLUMN_TOOLTIPS = {
    "operator": "Network operator name (e.g. \"AT&T\", \"MTN\").",
    "mccmnc": "MCC / MNC — Mobile Country Code / Mobile Network Code. "
              "Together they form the PLMN that uniquely identifies a "
              "carrier worldwide (e.g. 310/410 = AT&T US).",
    "rat": "Radio Access Technology — LTE, GSM, etc.",
    "pci": "Physical Cell ID (0–503). Distinguishes cells/sectors on the "
           "same tower at the same frequency.",
    "earfcn": "E-UTRA Absolute Radio Frequency Channel Number — the "
              "specific LTE downlink channel the cell is broadcasting on.",
    "cell_id": "Globally unique LTE cell identifier (hex). Encodes the "
               "eNodeB ID + sector index.",
    "tac": "Tracking Area Code — geographic grouping the cell belongs to. "
           "Used by the network for paging and mobility.",
    "rsrp": "Reference Signal Received Power (dBm). Per-resource-element "
            "LTE signal strength. Higher = better. Excellent ≥ −80, good "
            "−80 to −90, fair −90 to −100, poor ≤ −110.",
    "rsrq": "Reference Signal Received Quality (dB). Ratio of RSRP to RSSI "
            "— a quality metric. Higher = better. Excellent ≥ −10, fair "
            "−10 to −15, poor ≤ −20.",
    "rssi": "Received Signal Strength Indicator (dBm). Total in-band power "
            "including signal, noise, and interference. Less diagnostic "
            "than RSRP for LTE.",
    "sinr": "Signal-to-Interference-plus-Noise Ratio (dB). Higher = better. "
            "Excellent ≥ 20, good 13–20, fair 0–13, poor < 0.",
}


class ModemUI:
    def __init__(self, root):
        self.root = root
        self.root.title(f"EG915U Logger {__version__}")
        self.root.geometry("900x760")
        self.is_polling = False
        self.com_port = None
        self._ports = []

        # --- Device selection row ---
        device_row = ttk.Frame(root)
        device_row.pack(pady=(10, 0), padx=10, fill="x")
        ttk.Label(device_row, text="Device:").pack(side="left")
        self.device_combo = ttk.Combobox(device_row, state="readonly", width=45)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        tk.Button(device_row, text="Rescan", command=self.rescan_devices).pack(side="left")

        # --- Signal frame ---
        sig = ttk.LabelFrame(root, text="Signal (AT+CSQ)")
        sig.pack(pady=8, padx=10, fill="x")

        self.signal_bar = ttk.Progressbar(sig, mode="determinate", maximum=100, length=280)
        self.signal_bar.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")

        self.lbl_dbm = ttk.Label(sig, text="— dBm", font=("TkDefaultFont", 14, "bold"), width=12)
        self.lbl_dbm.grid(row=0, column=2, padx=8, pady=(8, 4), sticky="w")

        self.btn_poll = tk.Button(sig, text="Start Signal Polling", command=self.toggle_polling, width=22)
        self.btn_poll.grid(row=0, column=3, rowspan=2, padx=8, pady=6, sticky="e")

        self.lbl_bars = ttk.Label(sig, text="Bars: —")
        self.lbl_bars.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="w")

        self.lbl_ber = ttk.Label(sig, text="BER: —")
        self.lbl_ber.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="w")

        self.lbl_raw_csq = ttk.Label(sig, text="", foreground="#666")
        self.lbl_raw_csq.grid(row=1, column=2, padx=8, pady=(0, 8), sticky="w")

        sig.columnconfigure(3, weight=1)

        # --- Network Scan frame ---
        site = ttk.LabelFrame(root, text="Network Scan")
        site.pack(pady=4, padx=10, fill="x")

        self.lbl_context = ttk.Label(
            site,
            text="Click Full Network Scan to enumerate PLMNs.",
            foreground="#444",
            wraplength=860,
            justify="left",
        )
        self.lbl_context.grid(row=0, column=0, columnspan=3, padx=8, pady=(8, 4), sticky="w")

        columns = ("operator", "mccmnc", "rat", "pci", "earfcn", "cell_id", "tac", "rsrp", "rsrq", "rssi", "sinr")
        self.cells_tree = ttk.Treeview(site, columns=columns, show="headings", height=7)
        for col, text, width, anchor in [
            ("operator", "Operator", 110, "w"),
            ("mccmnc",   "MCC/MNC",  80,  "w"),
            ("rat",      "RAT",      60,  "w"),
            ("pci",      "PCI",      60,  "e"),
            ("earfcn",   "EARFCN",   80,  "e"),
            ("cell_id",  "Cell ID",  100, "w"),
            ("tac",      "TAC",      70,  "w"),
            ("rsrp",     "RSRP",     70,  "e"),
            ("rsrq",     "RSRQ",     70,  "e"),
            ("rssi",     "RSSI",     70,  "e"),
            ("sinr",     "SINR",     70,  "e"),
        ]:
            self.cells_tree.heading(col, text=text)
            self.cells_tree.column(col, width=width, anchor=anchor)
        self.cells_tree.tag_configure("serving", background="#d4edda")
        self.cells_tree.tag_configure("failed", background="#f8d7da")
        self.cells_tree.grid(row=1, column=0, columnspan=3, padx=8, pady=(4, 4), sticky="nsew")
        TreeHeaderTooltip(self.cells_tree, COLUMN_TOOLTIPS)

        self.btn_full_scan = tk.Button(site, text="Full Network Scan (slow, 3–8 min)", command=self.full_scan, width=32)
        self.btn_full_scan.grid(row=2, column=0, columnspan=2, padx=(8, 4), pady=(4, 8), sticky="w")

        self.lbl_scan_status = ttk.Label(site, text="", foreground="#666")
        self.lbl_scan_status.grid(row=2, column=2, padx=8, pady=(4, 8), sticky="w")

        site.columnconfigure(2, weight=1)

        # --- Console ---
        con = ttk.LabelFrame(root, text="Console")
        con.pack(pady=(4, 10), padx=10, fill="both", expand=True)
        self.log_area = scrolledtext.ScrolledText(con, width=80, height=12, state='disabled')
        self.log_area.pack(padx=6, pady=6, fill="both", expand=True)

        self.rescan_devices()

    def rescan_devices(self):
        ports = find_modem_ports()
        self._ports = ports
        labels = [f"{p.device} — {p.description}" for p in ports]
        self.device_combo["values"] = labels
        if ports:
            self.device_combo.current(0)
            self.com_port = ports[0].device
            self.log(f"Found {len(ports)} modem port(s). Selected {self.com_port}.")
        else:
            self.device_combo.set("")
            self.com_port = None
            self.log("No Quectel-like modem ports found. Plug in the device and click Rescan.")

    def _on_device_selected(self, _event):
        idx = self.device_combo.current()
        if 0 <= idx < len(self._ports):
            self.com_port = self._ports[idx].device
            self.log(f"Selected device: {self.com_port}")

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}\n"

        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, log_entry)
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

        with open("modem_log.txt", "a") as f:
            f.write(log_entry)

    def _set_signal(self, csq):
        if csq is None:
            self.signal_bar["value"] = 0
            self.lbl_dbm.config(text="— dBm")
            self.lbl_bars.config(text="Bars: —")
            self.lbl_ber.config(text="BER: —")
            self.lbl_raw_csq.config(text="")
            return
        dbm = csq["dbm"]
        bars = csq["bars"]
        pct = 0 if dbm is None else max(0, min(100, int((dbm + 113) * 100 / 62)))
        self.signal_bar["value"] = pct
        self.lbl_dbm.config(text=f"{dbm} dBm" if dbm is not None else "— dBm")
        filled = "●" * bars + "○" * (4 - bars)
        self.lbl_bars.config(text=f"Bars: {filled}")
        self.lbl_ber.config(text=f"BER: {csq['ber_text']}")
        self.lbl_raw_csq.config(text=f"rssi={csq['rssi']}")

    def _apply_full_scan(self, ctx, results):
        # Context strip — reflects state BEFORE the scan began (where the modem
        # was camped originally, since per-PLMN detail now lives per-row).
        parts = []
        cereg = ctx.get("cereg")
        if cereg:
            parts.append(f"Start: {cereg['stat_label']}")
            if cereg.get("tac"):
                parts.append(f"TAC {cereg['tac']}")
        qnw = ctx.get("qnwinfo")
        if qnw and qnw.get("act") and qnw["act"] != "No Service":
            oper = qnw.get("oper") or "?"
            parts.append(f"{qnw['act']} · {oper}")
            if qnw.get("band"):
                parts.append(qnw["band"])
            if qnw.get("channel") is not None:
                parts.append(f"ch {qnw['channel']}")
        self.lbl_context.config(text="  ·  ".join(parts) if parts else "No context available.")

        # Results table — one row per PLMN
        for iid in self.cells_tree.get_children():
            self.cells_tree.delete(iid)

        def fmt(v):
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:.0f}"
            return str(v)

        rich_count = 0
        for r in results:
            op = r["op"]
            serving = r.get("serving") or {}
            qcsq = r.get("qcsq") or {}
            csq = r.get("csq") or {}

            if r["status"] in ("ok", "neighbor"):
                rich_count += 1
                rsrp = serving.get("rsrp") if serving.get("rsrp") is not None else qcsq.get("rsrp")
                rsrq = serving.get("rsrq") if serving.get("rsrq") is not None else qcsq.get("rsrq")
                rssi = serving.get("rssi") if serving.get("rssi") is not None else qcsq.get("rssi")
                sinr = serving.get("sinr") if serving.get("sinr") is not None else qcsq.get("sinr")
                # GSM fallback: surface CSQ-derived dBm in the RSRP column
                if rsrp is None and csq.get("dbm") is not None:
                    rsrp = csq["dbm"]
                name = op["name"] + (" (nbr)" if r["status"] == "neighbor" else "")
                values = (
                    name,
                    op["mccmnc"],
                    op["act_label"],
                    fmt(serving.get("pci")),
                    fmt(serving.get("earfcn")),
                    serving.get("cell_id") or "—",
                    serving.get("tac") or "—",
                    fmt(rsrp),
                    fmt(rsrq),
                    fmt(rssi),
                    fmt(sinr),
                )
                if r["status"] == "ok" and op.get("stat") == 2:
                    tag = "serving"
                else:
                    tag = ""
            else:
                values = (
                    op["name"],
                    op["mccmnc"],
                    op["act_label"],
                    "—", "—", "—", "—", "—", "—", "—", "—",
                )
                # nosim: discovery-only (no SIM) — not a failure, just skipped.
                tag = "" if r["status"] == "nosim" else "failed"

            self.cells_tree.insert("", "end", values=values, tags=(tag,) if tag else ())

        plmn_count = len({(r["op"]["numeric"], r["op"]["act"]) for r in results})
        if any(r["status"] == "nosim" for r in results):
            self.lbl_scan_status.config(
                text=f"{rich_count} cell(s) · {plmn_count} PLMN(s) (no SIM)"
            )
        else:
            self.lbl_scan_status.config(text=f"{rich_count}/{plmn_count} PLMN(s) captured")

    def send_command(self, cmd, timeout=1, delay=0.5):
        if not self.com_port:
            return "Error: no device selected. Click Rescan and pick a port."
        try:
            with serial.Serial(self.com_port, BAUDRATE, timeout=timeout) as ser:
                ser.write((cmd + '\r\n').encode('utf-8'))
                time.sleep(delay)
                response = ser.read_all().decode('utf-8', errors='ignore').strip()
                lines = response.split('\n')
                cleaned_lines = [line.strip() for line in lines if line.strip() and cmd not in line and line.strip() != 'OK']
                return '\n'.join(cleaned_lines)
        except Exception as e:
            return f"Error opening {self.com_port}: {str(e)}"

    def poll_loop(self):
        self.log("Started polling AT+CSQ every 5 seconds.")
        while self.is_polling:
            response = self.send_command("AT+CSQ")
            if response and self.is_polling:
                self.log(f"Signal: {response}")
                csq = parse_csq(response)
                if csq:
                    self.root.after(0, self._set_signal, csq)
            time.sleep(5)

    def toggle_polling(self):
        if self.is_polling:
            self.is_polling = False
            self.btn_poll.config(text="Start Signal Polling")
            self.log("Stopped polling.")
        else:
            self.is_polling = True
            self.btn_poll.config(text="Stop Signal Polling")
            threading.Thread(target=self.poll_loop, daemon=True).start()

    def _wait_for_registration(self, timeout_s=20, poll_s=2):
        # Poll AT+CEREG? until registered (stat in 1/5), denied (3), or
        # timed out. Returns the final stat (int) or None for timeout.
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            c = parse_cereg(self.send_command("AT+CEREG?"))
            if c:
                last = c["stat"]
                if last in (1, 3, 5):
                    return last
            time.sleep(poll_s)
        return last  # may be 0/2/4 on timeout

    def _capture_current_cell(self):
        # Grab signal + serving-cell detail + QuecCell neighbour list for
        # whatever cell the modem is camped on right now (registered or
        # LIMSRV). Without a SIM, LIMSRV is the only way we can get rich
        # data on this modem, so this function is the core of the no-SIM
        # path. QCELLINFO needs a warm-up period to populate its cache.
        csq = parse_csq(self.send_command("AT+CSQ"))
        qcsq_resp = self.send_command("AT+QCSQ")
        self.log(f"QCSQ: {qcsq_resp}")
        qcsq = parse_qcsq(qcsq_resp)
        serving_resp = self.send_command('AT+QENG="servingcell"', timeout=3, delay=1.0)
        self.log(f"QENG servingcell: {serving_resp}")
        serving = parse_qeng_servingcell(serving_resp)

        # QuecCell: enable periodic cell info, wait for the cache to populate,
        # then query it. This returns serving + intra-freq neighbours.
        self.send_command("AT+QCELLINFO=1", timeout=3, delay=0.5)
        time.sleep(3)
        qcellinfo_resp = self.send_command("AT+QCELLINFO?", timeout=6, delay=2.0)
        self.log(f"QCELLINFO: {qcellinfo_resp}")
        cells = parse_qcellinfo(qcellinfo_resp)
        self.send_command("AT+QCELLINFO=0", timeout=3, delay=0.5)
        return csq, qcsq, serving, cells

    def _match_plmn(self, serving, ops):
        # Find the PLMN in ops whose numeric matches the MCC/MNC reported
        # by QENG. MNC can be 2 or 3 digits and COPS=? may report it either
        # zero-padded or not, so try all common variants.
        if not serving or serving.get("mcc") is None or serving.get("mnc") is None:
            return None
        mcc, mnc = serving["mcc"], serving["mnc"]
        candidates = {f"{mcc}{mnc:02d}", f"{mcc}{mnc:03d}", f"{mcc}{mnc}"}
        for op in ops:
            if op.get("numeric") in candidates:
                return (op["numeric"], op["act"])
        return None

    def _build_nosim_results(self, ops, csq, qcsq, qeng_serving, qcellinfo_cells):
        # Render one row per QCELLINFO cell whose MCC/MNC matches a PLMN
        # from discovery. Fall back to QENG's serving cell if QCELLINFO was
        # empty. Remaining PLMNs in the discovery list get a single dash
        # row apiece.
        results = []
        matched_keys = set()

        def _serving_dict_from_cell(cell):
            # QCELLINFO has no RSRQ; borrow it from QENG if PCI/EARFCN line
            # up with the serving cell we already have.
            rsrq = None
            if (qeng_serving and cell.get("pci") == qeng_serving.get("pci")
                    and cell.get("earfcn") == qeng_serving.get("earfcn")):
                rsrq = qeng_serving.get("rsrq")
            return {
                "rat": "LTE",
                "mcc": cell.get("mcc"),
                "mnc": cell.get("mnc"),
                "cell_id": cell.get("cell_id"),
                "pci": cell.get("pci"),
                "earfcn": cell.get("earfcn"),
                "tac": cell.get("tac"),
                "rsrp": cell.get("rsrp"),
                "rssi": cell.get("rssi"),
                "sinr": cell.get("sinr"),
                "rsrq": rsrq,
            }

        for cell in qcellinfo_cells or []:
            match = self._match_plmn(cell, ops)
            if not match:
                continue
            op = next(o for o in ops if (o["numeric"], o["act"]) == match)
            matched_keys.add(match)
            status = "ok" if cell["kind"] == "servingcell" else "neighbor"
            results.append({
                "op": op, "status": status,
                "csq": csq if status == "ok" else None,
                "qcsq": qcsq if status == "ok" else None,
                "serving": _serving_dict_from_cell(cell),
            })

        # QCELLINFO empty or yielded no matches? Fall back to the QENG
        # serving cell — one row for whichever PLMN it matches.
        if not results:
            match = self._match_plmn(qeng_serving, ops)
            if match:
                matched_keys.add(match)
                op = next(o for o in ops if (o["numeric"], o["act"]) == match)
                results.append({
                    "op": op, "status": "ok",
                    "csq": csq, "qcsq": qcsq, "serving": qeng_serving,
                })

        if matched_keys:
            joined = ", ".join(k[0] for k in matched_keys)
            self.log(f"Enriched {len(results)} row(s) from LIMSRV capture: {joined}")
        else:
            self.log("No LIMSRV cell matched any discovered PLMN — discovery-only.")

        for op in ops:
            if (op["numeric"], op["act"]) not in matched_keys:
                results.append({"op": op, "status": "nosim"})
        return results

    def _qnwlock_sweep(self, ops, known_results):
        # Force-camp the modem on each candidate LTE EARFCN via AT+QNWLOCK
        # and capture whatever LIMSRV cell it lands on. Without a SIM this
        # is the only way to get rich data on cells outside the band the
        # modem happened to start on. Dedupes against cells already in
        # known_results; returns only new rows.
        def _key(c):
            return (c.get("mcc"), c.get("mnc"), c.get("pci"),
                    c.get("earfcn"), c.get("cell_id"))

        seen = {
            _key(r["serving"]) for r in known_results
            if r.get("serving") and r["status"] in ("ok", "neighbor")
        }
        new_results = []
        try:
            for i, (earfcn, band_label) in enumerate(LTE_SWEEP_EARFCNS):
                self.lbl_scan_status.config(
                    text=f"Band sweep [{i + 1}/{len(LTE_SWEEP_EARFCNS)}] "
                         f"{band_label} EARFCN {earfcn}…"
                )
                self.log(f"Sweep {band_label}: locking to EARFCN {earfcn}…")
                lock_resp = self.send_command(
                    f'AT+QNWLOCK="lte",1,{earfcn}', timeout=8, delay=1.5
                )
                if "ERROR" in (lock_resp or "").upper():
                    self.log(f"  QNWLOCK rejected: {lock_resp.strip()}")
                    continue
                # Wait for the modem to scan the channel and (maybe) camp.
                time.sleep(6)
                csq, qcsq, serving, cells = self._capture_current_cell()

                added_here = 0
                # Prefer QCELLINFO — it carries MCC/MNC and may include
                # neighbour cells on the same locked frequency.
                for cell in cells or []:
                    match = self._match_plmn(cell, ops)
                    if not match:
                        continue
                    k = _key(cell)
                    if k in seen:
                        continue
                    seen.add(k)
                    op = next(o for o in ops
                              if (o["numeric"], o["act"]) == match)
                    status = "ok" if cell["kind"] == "servingcell" else "neighbor"
                    rsrq = (serving.get("rsrq") if serving
                            and cell.get("pci") == serving.get("pci")
                            and cell.get("earfcn") == serving.get("earfcn")
                            else None)
                    new_results.append({
                        "op": op, "status": status,
                        "csq": csq if status == "ok" else None,
                        "qcsq": qcsq if status == "ok" else None,
                        "serving": {
                            "rat": "LTE",
                            "mcc": cell.get("mcc"),
                            "mnc": cell.get("mnc"),
                            "cell_id": cell.get("cell_id"),
                            "pci": cell.get("pci"),
                            "earfcn": cell.get("earfcn"),
                            "tac": cell.get("tac"),
                            "rsrp": cell.get("rsrp"),
                            "rssi": cell.get("rssi"),
                            "sinr": cell.get("sinr"),
                            "rsrq": rsrq,
                        },
                    })
                    added_here += 1

                # Fallback: QCELLINFO empty but modem did camp — take QENG.
                if added_here == 0 and serving:
                    match = self._match_plmn(serving, ops)
                    if match and _key(serving) not in seen:
                        seen.add(_key(serving))
                        op = next(o for o in ops
                                  if (o["numeric"], o["act"]) == match)
                        new_results.append({
                            "op": op, "status": "ok",
                            "csq": csq, "qcsq": qcsq, "serving": serving,
                        })
                        added_here += 1

                if added_here:
                    self.log(f"  Sweep {band_label}: +{added_here} cell(s)")
                else:
                    self.log(f"  Sweep {band_label}: no new cells")
        finally:
            self.send_command('AT+QNWLOCK="lte",0', timeout=5, delay=1.0)
            self.log("Released QNWLOCK (automatic band selection restored).")
        return new_results

    def full_scan_loop(self):
        self.btn_full_scan.config(state=tk.DISABLED)
        self.btn_poll.config(state=tk.DISABLED)
        try:
            # Pre-loop: SIM state. Missing SIM (CME 10) is OK — we can still
            # do discovery. Other non-READY states (PIN/PUK/failure) abort.
            cpin_resp = self.send_command("AT+CPIN?")
            self.log(f"CPIN: {cpin_resp}")
            cpin_up = (cpin_resp or "").upper()
            sim_ready = "READY" in cpin_up
            sim_missing = parse_cme_error(cpin_resp) == 10
            if not sim_ready and not sim_missing:
                self.log("SIM not READY (locked or failure) — aborting scan.")
                self.lbl_scan_status.config(text="SIM not ready — see console")
                return
            if sim_missing:
                self.log("No SIM detected. Will run discovery only — "
                         "per-PLMN signal/cell detail requires a SIM.")

            # Probe AT+QNWLOCK support. If the firmware accepts it, future
            # work can drive per-(EARFCN, PCI) camping without a SIM. Just
            # log the result for now.
            qnwlock_probe = self.send_command("AT+QNWLOCK=?", timeout=3, delay=0.5)
            self.log(f"QNWLOCK probe: {qnwlock_probe or '(no response)'}")

            cereg_resp = self.send_command("AT+CEREG?")
            self.log(f"CEREG: {cereg_resp}")
            cereg = parse_cereg(cereg_resp)

            qnwinfo_resp = self.send_command("AT+QNWINFO")
            self.log(f"QNWINFO: {qnwinfo_resp}")
            qnwinfo = parse_qnwinfo(qnwinfo_resp)

            # Snapshot whatever cell the modem is camped on right now — this
            # is the "LIMSRV" cell without a SIM, or the registered cell with
            # one. Done BEFORE COPS=? because the 2-minute scan puts the
            # modem into a transient searching state afterwards.
            initial_csq, initial_qcsq, initial_serving, initial_cells = (
                self._capture_current_cell()
            )

            # PLMN discovery
            self.lbl_scan_status.config(text="PLMN discovery… up to 3 min")
            self.log("PLMN discovery (AT+COPS=?) started. May take up to 3 minutes.")
            cops_resp = self.send_command("AT+COPS=?", timeout=180, delay=120)
            self.log(f"COPS=? raw: {cops_resp}")
            all_ops = parse_cops(cops_resp)
            for op in all_ops:
                self.log(
                    f"  {op['stat_label']:<10} {op['name']:<18} "
                    f"{op['mccmnc']:<10} {op['act_label']}"
                )
            ops = dedupe_plmns(all_ops)
            ctx = {"cereg": cereg, "qnwinfo": qnwinfo}

            if not ops:
                self.lbl_scan_status.config(text="No PLMNs found")
                self.log("No non-Forbidden PLMNs found; nothing to display.")
                self.root.after(0, self._apply_full_scan, ctx, [])
                return

            # No-SIM path: no registration possible, but the LIMSRV-camped
            # cell (and any intra-freq neighbours QCELLINFO decoded) give us
            # rich data for a subset of PLMNs. If QNWLOCK is supported, we
            # additionally sweep through representative EARFCNs across bands
            # to force re-camping on other cells.
            if sim_missing:
                results = self._build_nosim_results(
                    ops, initial_csq, initial_qcsq, initial_serving, initial_cells
                )
                if "ERROR" not in (qnwlock_probe or "").upper():
                    self.log(
                        f"Starting QNWLOCK band sweep across "
                        f"{len(LTE_SWEEP_EARFCNS)} candidate EARFCN(s) "
                        f"(~{len(LTE_SWEEP_EARFCNS) * 12}s)…"
                    )
                    sweep_results = self._qnwlock_sweep(ops, results)
                    if sweep_results:
                        newly_matched = {
                            (r["op"]["numeric"], r["op"]["act"])
                            for r in sweep_results
                        }
                        # Drop discovery-only rows we now have real data for.
                        results = [
                            r for r in results
                            if not (r["status"] == "nosim"
                                    and (r["op"]["numeric"], r["op"]["act"])
                                    in newly_matched)
                        ] + sweep_results
                        self.log(
                            f"Sweep added {len(sweep_results)} cell(s)."
                        )
                    else:
                        self.log("Sweep yielded no additional cells.")
                else:
                    self.log("Skipping band sweep — QNWLOCK unsupported.")
                self.root.after(0, self._apply_full_scan, ctx, results)
                rich = sum(1 for r in results if r["status"] in ("ok", "neighbor"))
                self.lbl_scan_status.config(
                    text=f"{rich} cell(s) · {len(ops)} PLMN(s) (no SIM)"
                )
                return

            # Per-PLMN registration + detail capture (SIM present)
            results = []
            try:
                for i, op in enumerate(ops):
                    self.lbl_scan_status.config(
                        text=f"[{i + 1}/{len(ops)}] {op['name']} ({op['act_label']})…"
                    )
                    # Deregister first. A prior failed COPS=1 leaves the modem
                    # in a state where subsequent manual attempts fast-fail
                    # with CME ERROR 10 until explicitly deregistered.
                    self.send_command("AT+COPS=2", timeout=10, delay=1.0)

                    self.log(
                        f"Registering to {op['name']} {op['mccmnc']} ({op['act_label']})…"
                    )
                    reg_cmd = f'AT+COPS=1,2,"{op["numeric"]}",{op["act"]}'
                    reg_resp = self.send_command(reg_cmd, timeout=35, delay=2.0)
                    if reg_resp:
                        self.log(f"  COPS reg: {reg_resp}")

                    # Some Quectel firmware rejects the AcT argument — retry
                    # once without it before giving up.
                    if "ERROR" in reg_resp.upper() and parse_cme_error(reg_resp) == 10:
                        self.send_command("AT+COPS=2", timeout=10, delay=1.0)
                        self.log("  Retrying without AcT parameter…")
                        reg_cmd = f'AT+COPS=1,2,"{op["numeric"]}"'
                        reg_resp = self.send_command(reg_cmd, timeout=35, delay=2.0)
                        if reg_resp:
                            self.log(f"  COPS reg retry: {reg_resp}")

                    if "ERROR" in reg_resp.upper():
                        code = parse_cme_error(reg_resp)
                        results.append({"op": op, "status": "failed", "err": code})
                        continue

                    # AT+COPS=1 is supposed to block until registration resolves,
                    # but some firmwares return OK early. Poll CEREG to confirm.
                    stat = self._wait_for_registration(timeout_s=20)
                    self.log(f"  CEREG after reg: stat={stat}")
                    if stat not in (1, 5):
                        results.append({"op": op, "status": "failed",
                                        "err": f"cereg_stat={stat}"})
                        continue

                    csq = parse_csq(self.send_command("AT+CSQ"))
                    qcsq = None
                    serving = None
                    if op["act"] == 7:  # LTE
                        qcsq_resp = self.send_command("AT+QCSQ")
                        self.log(f"  QCSQ: {qcsq_resp}")
                        qcsq = parse_qcsq(qcsq_resp)

                        serving_resp = self.send_command(
                            'AT+QENG="servingcell"', timeout=3, delay=1.0
                        )
                        self.log(f"  QENG servingcell: {serving_resp}")
                        serving = parse_qeng_servingcell(serving_resp)

                    results.append({
                        "op": op, "status": "ok",
                        "csq": csq, "qcsq": qcsq, "serving": serving,
                    })
            finally:
                # Always restore automatic PLMN selection, even if the loop
                # raised. Leaving the modem in manual mode would strand it on
                # the last-attempted PLMN.
                self.send_command("AT+COPS=0", timeout=35, delay=2.0)
                self.log("Restored automatic PLMN selection (AT+COPS=0).")

            self.root.after(0, self._apply_full_scan, ctx, results)
            ok = sum(1 for r in results if r["status"] == "ok")
            self.log(f"Full scan complete: {ok}/{len(results)} PLMN(s) captured.")
        finally:
            self.btn_full_scan.config(state=tk.NORMAL)
            self.btn_poll.config(state=tk.NORMAL)

    def full_scan(self):
        if self.is_polling:
            self.log("Error: Stop signal polling before running a network scan.")
            return
        threading.Thread(target=self.full_scan_loop, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = ModemUI(root)
    root.mainloop()
