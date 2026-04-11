import tkinter as tk
from tkinter import scrolledtext, ttk
import serial
from serial.tools import list_ports
import threading
import time
import datetime
import re

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


def parse_qeng_neighbourcell(response):
    out = []
    for line in (response or '').splitlines():
        m = re.match(r'\+QENG:\s*"neighbourcell(?:\s+(\w+))?","(\w+)",(.*)', line)
        if not m:
            continue
        subtype = m.group(1) or ""
        rat = m.group(2)
        fields = _split_csv_quoted(m.group(3))

        def _val(i):
            return fields[i] if i < len(fields) else None

        if rat == "LTE":
            out.append({
                "subtype": subtype,
                "rat": "LTE",
                "earfcn": _int_or_none(_val(0)),
                "pci": _int_or_none(_val(1)),
                "rsrp": _int_or_none(_val(2)),
                "rsrq": _int_or_none(_val(3)),
                "rssi": _int_or_none(_val(4)),
                "sinr": qeng_sinr_to_db(_int_or_none(_val(5))),
                "srxlev": _int_or_none(_val(6)),
            })
    return out


def parse_qcellinfo(response):
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


class ModemUI:
    def __init__(self, root):
        self.root = root
        self.root.title("EG915U Logger")
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

        # --- Site Scan frame ---
        site = ttk.LabelFrame(root, text="Site Scan")
        site.pack(pady=4, padx=10, fill="x")

        self.lbl_context = ttk.Label(
            site,
            text="Click Site Scan to enumerate cells.",
            foreground="#444",
            wraplength=860,
            justify="left",
        )
        self.lbl_context.grid(row=0, column=0, columnspan=3, padx=8, pady=(8, 4), sticky="w")

        columns = ("type", "mccmnc", "pci", "earfcn", "cell_id", "tac", "rsrp", "rsrq", "sinr")
        self.cells_tree = ttk.Treeview(site, columns=columns, show="headings", height=7)
        for col, text, width, anchor in [
            ("type",    "Type",    80,  "w"),
            ("mccmnc",  "MCC/MNC", 80,  "w"),
            ("pci",     "PCI",     60,  "e"),
            ("earfcn",  "EARFCN",  80,  "e"),
            ("cell_id", "Cell ID", 100, "w"),
            ("tac",     "TAC",     70,  "w"),
            ("rsrp",    "RSRP",    70,  "e"),
            ("rsrq",    "RSRQ",    70,  "e"),
            ("sinr",    "SINR",    70,  "e"),
        ]:
            self.cells_tree.heading(col, text=text)
            self.cells_tree.column(col, width=width, anchor=anchor)
        self.cells_tree.tag_configure("serving", background="#d4edda")
        self.cells_tree.grid(row=1, column=0, columnspan=3, padx=8, pady=(4, 4), sticky="nsew")

        self.btn_site_scan = tk.Button(site, text="Site Scan", command=self.site_scan, width=18)
        self.btn_site_scan.grid(row=2, column=0, padx=(8, 4), pady=(4, 8), sticky="w")

        self.btn_plmn_scan = tk.Button(site, text="PLMN Scan (slow, 1-3 min)", command=self.plmn_scan, width=26)
        self.btn_plmn_scan.grid(row=2, column=1, padx=4, pady=(4, 8), sticky="w")

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

    def _apply_site_scan(self, ctx, cells):
        # Context strip
        parts = []
        cereg = ctx.get("cereg")
        if cereg:
            parts.append(f"State: {cereg['stat_label']}")
            if cereg.get("tac"):
                parts.append(f"TAC {cereg['tac']}")
        qnw = ctx.get("qnwinfo")
        if qnw and qnw.get("act") and qnw["act"] != "No Service":
            mccmnc = qnw.get("oper") or "?"
            parts.append(f"{qnw['act']} · {mccmnc}")
            if qnw.get("band"):
                parts.append(qnw["band"])
            if qnw.get("channel") is not None:
                parts.append(f"ch {qnw['channel']}")
        qcsq = ctx.get("qcsq")
        if qcsq and qcsq.get("sysmode") == "LTE":
            sig_parts = []
            if qcsq.get("rsrp") is not None:
                sig_parts.append(f"RSRP {qcsq['rsrp']}")
            if qcsq.get("rsrq") is not None:
                sig_parts.append(f"RSRQ {qcsq['rsrq']}")
            if qcsq.get("sinr") is not None:
                sig_parts.append(f"SINR {qcsq['sinr']:.1f}")
            if sig_parts:
                parts.append(" / ".join(sig_parts))
        self.lbl_context.config(text="  ·  ".join(parts) if parts else "No context available.")

        # Cells table
        for iid in self.cells_tree.get_children():
            self.cells_tree.delete(iid)

        def fmt(v):
            if v is None:
                return "—"
            if isinstance(v, float):
                return f"{v:.0f}"
            return str(v)

        for cell in cells:
            mcc, mnc = cell.get("mcc"), cell.get("mnc")
            if mcc is not None and mnc is not None:
                mccmnc = f"{mcc}/{mnc:02d}"
            else:
                mccmnc = "—"
            kind_label = "Serving" if cell["kind"] == "servingcell" else "Neighbor"
            tag = "serving" if cell["kind"] == "servingcell" else ""
            self.cells_tree.insert(
                "", "end",
                values=(
                    kind_label,
                    mccmnc,
                    fmt(cell.get("pci")),
                    fmt(cell.get("earfcn")),
                    cell.get("cell_id") or "—",
                    cell.get("tac") or "—",
                    fmt(cell.get("rsrp")),
                    fmt(cell.get("rsrq")),
                    fmt(cell.get("sinr")),
                ),
                tags=(tag,) if tag else (),
            )
        self.lbl_scan_status.config(text=f"{len(cells)} cell(s)")

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

    def site_scan_loop(self):
        self.btn_site_scan.config(state=tk.DISABLED)
        self.btn_plmn_scan.config(state=tk.DISABLED)
        self.btn_poll.config(state=tk.DISABLED)
        self.lbl_scan_status.config(text="Scanning site…")
        self.log("Site scan started.")
        try:
            cereg_resp = self.send_command("AT+CEREG?")
            self.log(f"CEREG: {cereg_resp}")
            cereg = parse_cereg(cereg_resp)

            qnwinfo_resp = self.send_command("AT+QNWINFO")
            self.log(f"QNWINFO: {qnwinfo_resp}")
            qnwinfo = parse_qnwinfo(qnwinfo_resp)

            qcsq_resp = self.send_command("AT+QCSQ")
            self.log(f"QCSQ: {qcsq_resp}")
            qcsq = parse_qcsq(qcsq_resp)

            serving_resp = self.send_command('AT+QENG="servingcell"', timeout=3, delay=1.0)
            self.log(f"QENG servingcell: {serving_resp}")
            serving = parse_qeng_servingcell(serving_resp)

            qcellinfo_resp = self.send_command("AT+QCELLINFO?", timeout=6, delay=2.0)
            self.log(f"QCELLINFO: {qcellinfo_resp}")
            cells = parse_qcellinfo(qcellinfo_resp)

            neigh_resp = self.send_command('AT+QENG="neighbourcell"', timeout=5, delay=2.0)
            self.log(f"QENG neighbourcell: {neigh_resp}")
            neighbors = parse_qeng_neighbourcell(neigh_resp)

            # Enrich QCELLINFO cells with RSRQ/srxlev from QENG (matched by PCI + EARFCN)
            neigh_by_key = {
                (n["pci"], n["earfcn"]): n for n in neighbors if n.get("pci") is not None
            }
            for c in cells:
                key = (c.get("pci"), c.get("earfcn"))
                n = neigh_by_key.get(key)
                c["rsrq"] = n["rsrq"] if n else None
                c["srxlev"] = n["srxlev"] if n else None

            # Serving cell RSRQ/srxlev comes from QENG servingcell, not neighbours
            if serving and serving.get("rat") == "LTE":
                for c in cells:
                    if c["kind"] == "servingcell":
                        if c.get("rsrq") is None:
                            c["rsrq"] = serving.get("rsrq")
                        if c.get("srxlev") is None:
                            c["srxlev"] = serving.get("srxlev")

            # If QCELLINFO returned nothing but QENG servingcell did, synthesize
            # a single serving row so the user sees something.
            if not cells and serving and serving.get("rat") == "LTE" and serving.get("pci") is not None:
                cells = [{
                    "kind": "servingcell",
                    "rat": "LTE",
                    "mcc": serving.get("mcc"),
                    "mnc": serving.get("mnc"),
                    "tac": serving.get("tac"),
                    "cell_id": serving.get("cell_id"),
                    "pci": serving.get("pci"),
                    "earfcn": serving.get("earfcn"),
                    "rsrp": serving.get("rsrp"),
                    "rsrq": serving.get("rsrq"),
                    "rssi": serving.get("rssi"),
                    "sinr": serving.get("sinr"),
                    "srxlev": serving.get("srxlev"),
                }]

            ctx = {"cereg": cereg, "qnwinfo": qnwinfo, "qcsq": qcsq, "serving": serving}
            self.root.after(0, self._apply_site_scan, ctx, cells)
            self.log(f"Site scan complete: {len(cells)} cell(s).")
        finally:
            self.btn_site_scan.config(state=tk.NORMAL)
            self.btn_plmn_scan.config(state=tk.NORMAL)
            self.btn_poll.config(state=tk.NORMAL)

    def site_scan(self):
        if self.is_polling:
            self.log("Error: Stop signal polling before running a site scan.")
            return
        threading.Thread(target=self.site_scan_loop, daemon=True).start()

    def plmn_scan_loop(self):
        self.btn_site_scan.config(state=tk.DISABLED)
        self.btn_plmn_scan.config(state=tk.DISABLED)
        self.btn_poll.config(state=tk.DISABLED)
        self.lbl_scan_status.config(text="PLMN scan… up to 3 min")
        self.log("PLMN scan (AT+COPS=?) started. May take up to 3 minutes.")
        try:
            response = self.send_command("AT+COPS=?", timeout=180, delay=120)
            self.log(f"COPS=? raw: {response}")
            ops = parse_cops(response)
            if ops:
                for op in ops:
                    self.log(
                        f"  {op['stat_label']:<10} {op['name']:<18} "
                        f"{op['mccmnc']:<10} {op['act_label']}"
                    )
                self.lbl_scan_status.config(text=f"{len(ops)} PLMN(s) visible")
            elif '+COPS:' not in (response or ''):
                self.lbl_scan_status.config(text="PLMN scan failed — see console")
            else:
                self.lbl_scan_status.config(text="No PLMNs visible")
        finally:
            self.btn_site_scan.config(state=tk.NORMAL)
            self.btn_plmn_scan.config(state=tk.NORMAL)
            self.btn_poll.config(state=tk.NORMAL)

    def plmn_scan(self):
        if self.is_polling:
            self.log("Error: Stop signal polling before running a PLMN scan.")
            return
        threading.Thread(target=self.plmn_scan_loop, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = ModemUI(root)
    root.mainloop()
