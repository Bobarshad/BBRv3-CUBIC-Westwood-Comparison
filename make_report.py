#!/usr/bin/env python3
"""Build Excel workbook + PNG charts from results.jsonl."""
import json, sys, os, statistics
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.utils import get_column_letter
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

src, outdir = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(src)]
CCAS = ["cubic", "bbr", "westwood"]
LABEL = {"cubic": "CUBIC", "bbr": "BBRv3", "westwood": "Westwood+"}
COLOR = {"cubic": "2a78d6", "bbr": "eb6834", "westwood": "1baf7a"}
LOSSES = sorted({r["loss_pct"] for r in rows})
DELAYS = sorted({r["delay_ms"] for r in rows})
TOOLS = ["iperf3", "iperf2"]
idx = {(r["tool"], r["delay_ms"], r["loss_pct"], r["cca"]): r for r in rows}

def val(tool, d, l, c, key):
    r = idx.get((tool, d, l, c))
    if not r or not r.get("ok"):
        return None
    v = r.get(key)
    return round(v, 2) if isinstance(v, float) else v

def retr_pct(tool, d, l, c):
    r = idx.get((tool, d, l, c))
    if not r or not r.get("ok") or not r.get("kernel_out_segs"):
        return None
    return round(100.0 * r["kernel_retrans_segs"] / r["kernel_out_segs"], 3)

HDR = Font(bold=True, color="FFFFFF"); HFILL = PatternFill("solid", fgColor="404040")
THIN = Side(style="thin", color="BBBBBB"); BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

def style_header(ws, row, ncol):
    for c in range(1, ncol + 1):
        cell = ws.cell(row=row, column=c); cell.font = HDR; cell.fill = HFILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

wb = Workbook()
ws = wb.active; ws.title = "README"
info = [
    ("TCP CCA comparison on FrGW-02 (BBRv3 kernel 6.13.7-bbr3+)", ""),
    ("Sender / tc host", "FrGW-02 192.168.1.170 (br0 = enp1s0f0 + enp1s0f1, Intel 82599ES 10G)"),
    ("Receiver", "192.168.1.159 (iperf3 :5201, iperf2 :5001); path bottleneck ~1 Gbit/s"),
    ("Direction", "Upload: FrGW-02 -> 192.168.1.159 (sender-side CCA under test)"),
    ("CCAs", "cubic, bbr (BBR v3, module version 3), westwood (Westwood+)"),
    ("Impairment", "tc netem on FrGW-02, applied to traffic to/from 192.168.1.159 only: "
                   "egress = br0 prio qdisc + u32 dst filter -> netem; ingress = br0 ingress u32 src filter -> mirred -> ifb0 netem"),
    ("Loss/delay semantics", "Same loss % and same delay on BOTH egress and ingress: added RTT = 2 x delay; "
                             "data packets AND ACKs both see the loss %"),
    ("Loss values (%)", ", ".join(str(x) for x in LOSSES)),
    ("Delay values (ms, each way)", ", ".join(str(x) for x in DELAYS)),
    ("Test", "Single TCP stream, 10 s per run, one run per tool per cell; iperf3 3.18 (-C cca) and iperf2 2.2.1 (-Z cca)"),
    ("Accuracy fix", "br0 gso_max_segs=1 + TSO/GSO/GRO off during tests so netem drops individual packets, not 64KB GSO super-packets"),
    ("Retrans %", "kernel TcpRetransSegs / TcpOutSegs delta (nstat) during the run"),
    ("Measured loss", "netem drop counters per run (sheet Measured_Loss) - confirms applied loss rate"),
    ("Runs total", str(len(rows))),
    ("Failed runs", str(sum(1 for r in rows if not r.get("ok")))),
]
for i, (k, v) in enumerate(info, 1):
    ws.cell(row=i, column=1, value=k).font = Font(bold=True, size=14 if i == 1 else 11)
    ws.cell(row=i, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="top")
ws.column_dimensions["A"].width = 28; ws.column_dimensions["B"].width = 120

# Raw data
wr = wb.create_sheet("Raw_Data")
cols = ["tool", "cca", "delay_ms", "loss_pct", "ok", "mbps", "retransmits", "kernel_out_segs",
        "kernel_retrans_segs", "mean_rtt_us", "min_rtt_us", "max_rtt_us", "max_cwnd_bytes",
        "egress_pkts", "egress_dropped", "egress_loss_pct", "ingress_pkts", "ingress_dropped",
        "ingress_loss_pct", "attempts", "start", "error"]
wr.append(cols); style_header(wr, 1, len(cols))
for r in sorted(rows, key=lambda r: (r["tool"], r["delay_ms"], r["loss_pct"], CCAS.index(r["cca"]))):
    wr.append([round(r[c], 3) if isinstance(r.get(c), float) else r.get(c) for c in cols])
wr.freeze_panes = "A2"; wr.auto_filter.ref = wr.dimensions
for i in range(1, len(cols) + 1):
    wr.column_dimensions[get_column_letter(i)].width = 14

def pivot_sheet(name, title, fn, fmt="0.0", scale=True, chart_ylabel=None):
    """rows = loss, column blocks per delay with 3 CCAs; optional line chart per delay."""
    s = wb.create_sheet(name)
    s.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    # header rows 2-3
    s.cell(row=2, column=1, value="Delay (ms each way) ->"); s.cell(row=3, column=1, value="Loss %")
    col = 2
    for d in DELAYS:
        s.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col + 2)
        s.cell(row=2, column=col, value=f"{d} ms (+{2*d} ms RTT)")
        for j, c in enumerate(CCAS):
            s.cell(row=3, column=col + j, value=LABEL[c])
        col += 3
    style_header(s, 2, col - 1); style_header(s, 3, col - 1)
    for i, l in enumerate(LOSSES):
        rr = 4 + i
        s.cell(row=rr, column=1, value=l).number_format = "0.00"
        col = 2
        for d in DELAYS:
            for j, c in enumerate(CCAS):
                cell = s.cell(row=rr, column=col + j, value=fn(d, l, c))
                cell.number_format = fmt; cell.border = BOX
            col += 3
    last = 3 + len(LOSSES)
    if scale:
        rng = f"B4:{get_column_letter(1 + 3*len(DELAYS))}{last}"
        s.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color="F8F8F8",
                                                         end_type="max", end_color="5B9BD5"))
    s.column_dimensions["A"].width = 22
    for i in range(2, 2 + 3*len(DELAYS)):
        s.column_dimensions[get_column_letter(i)].width = 11
    s.freeze_panes = "B4"
    if chart_ylabel:
        for k, d in enumerate(DELAYS):
            ch = LineChart(); ch.title = f"{title.split(' (')[0]} - delay {d} ms"
            ch.y_axis.title = chart_ylabel; ch.x_axis.title = "Loss % (each direction)"
            ch.height, ch.width = 8, 16
            c0 = 2 + 3*k
            data = Reference(s, min_col=c0, max_col=c0 + 2, min_row=3, max_row=last)
            ch.add_data(data, titles_from_data=True)
            ch.set_categories(Reference(s, min_col=1, min_row=4, max_row=last))
            for j, ser in enumerate(ch.series):
                ser.graphicalProperties.line.solidFill = COLOR[CCAS[j]]
                ser.graphicalProperties.line.width = 25400  # 2pt
                ser.marker.symbol = "circle"; ser.marker.size = 6
                ser.marker.graphicalProperties = GraphicalProperties(solidFill=COLOR[CCAS[j]])
                ser.smooth = False
            ch.x_axis.delete = False; ch.y_axis.delete = False
            anchor_col = get_column_letter(2 + 3*len(DELAYS) + 1 + (k % 2) * 10)
            anchor_row = 2 + (k // 2) * 17
            s.add_chart(ch, f"{anchor_col}{anchor_row}")
    return s

for tool in TOOLS:
    pivot_sheet(f"{tool}_Throughput", f"{tool} throughput (Mbit/s)",
                lambda d, l, c, t=tool: val(t, d, l, c, "mbps"), chart_ylabel="Mbit/s")
pivot_sheet("iperf3_Retrans_pct", "iperf3 retransmitted segments (% of sent, kernel counters)",
            lambda d, l, c: retr_pct("iperf3", d, l, c), fmt="0.000", chart_ylabel="Retrans %")
pivot_sheet("iperf3_MeanRTT_ms", "iperf3 mean smoothed RTT (ms)",
            lambda d, l, c: (val("iperf3", d, l, c, "mean_rtt_us") or 0) / 1000 if val("iperf3", d, l, c, "mean_rtt_us") is not None else None,
            fmt="0.00", chart_ylabel="ms")
pivot_sheet("iperf3_MaxCwnd_KB", "iperf3 max congestion window (KB)",
            lambda d, l, c: round(val("iperf3", d, l, c, "max_cwnd_bytes") / 1024, 1) if val("iperf3", d, l, c, "max_cwnd_bytes") is not None else None,
            fmt="0.0")

def ratio(d, l, c, base):
    a = val("iperf3", d, l, c, "mbps"); b = val("iperf3", d, l, base, "mbps")
    return round(a / b, 2) if a and b else None
s = wb.create_sheet("BBRv3_vs_others")
s.cell(row=1, column=1, value="iperf3 throughput ratio: BBRv3 / CUBIC and BBRv3 / Westwood+").font = Font(bold=True, size=13)
s.cell(row=2, column=1, value="Loss %")
col = 2
for d in DELAYS:
    s.cell(row=2, column=col, value=f"d={d}ms bbr/cubic"); s.cell(row=2, column=col + 1, value=f"d={d}ms bbr/westwood"); col += 2
style_header(s, 2, col - 1)
for i, l in enumerate(LOSSES):
    s.cell(row=3 + i, column=1, value=l)
    col = 2
    for d in DELAYS:
        s.cell(row=3 + i, column=col, value=ratio(d, l, "bbr", "cubic")).number_format = "0.00x"
        s.cell(row=3 + i, column=col + 1, value=ratio(d, l, "bbr", "westwood")).number_format = "0.00x"
        col += 2
s.conditional_formatting.add(f"B3:{get_column_letter(col-1)}{2+len(LOSSES)}",
    ColorScaleRule(start_type="num", start_value=0.5, start_color="F4B183", mid_type="num", mid_value=1,
                   mid_color="FFFFFF", end_type="max", end_color="70AD47"))
for i in range(1, col): s.column_dimensions[get_column_letter(i)].width = 17

# Measured loss
m = wb.create_sheet("Measured_Loss")
m.cell(row=1, column=1, value="Configured vs measured netem loss (aggregated over all runs at that loss %)").font = Font(bold=True, size=13)
m.append(["Configured loss %", "Egress pkts", "Egress dropped", "Measured egress loss %",
          "Ingress pkts", "Ingress dropped", "Measured ingress loss %"]); style_header(m, 2, 7)
for l in LOSSES:
    rs = [r for r in rows if r["loss_pct"] == l and r.get("egress_pkts") is not None]
    ep = sum(r["egress_pkts"] for r in rs); ed = sum(r["egress_dropped"] for r in rs)
    ip_ = sum(r["ingress_pkts"] for r in rs); idr = sum(r["ingress_dropped"] for r in rs)
    m.append([l, ep, ed, round(100*ed/ep, 4) if ep else None, ip_, idr, round(100*idr/ip_, 4) if ip_ else None])
for i in range(1, 8): m.column_dimensions[get_column_letter(i)].width = 20

os.makedirs(outdir, exist_ok=True)
xlsx = os.path.join(outdir, "FrGW-02_CCA_comparison.xlsx"); wb.save(xlsx); print("wrote", xlsx)

# ---- PNG charts for the MD report (small multiples, one per delay)
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.edgecolor": "#888", "axes.labelcolor": "#333", "xtick.color": "#555",
                     "ytick.color": "#555", "axes.grid": True, "grid.color": "#e5e5e5", "grid.linewidth": 0.8})
xs = list(range(len(LOSSES))); xl = [f"{l:g}" for l in LOSSES]
def multiples(metric_fn, ylabel, fname, title, logy=False, tool="iperf3"):
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), sharey=True)
    for ax, d in zip(axes.flat, DELAYS):
        for c in CCAS:
            ys = [metric_fn(tool, d, l, c) for l in LOSSES]
            ax.plot(xs, [y if y is not None else float("nan") for y in ys], color="#" + COLOR[c],
                    lw=2, marker="o", ms=5, label=LABEL[c], markeredgecolor="white", markeredgewidth=1)
        ax.set_title(f"delay {d} ms each way (+{2*d} ms RTT)", fontsize=11, loc="left", color="#222")
        ax.set_xticks(xs); ax.set_xticklabels(xl, rotation=45, fontsize=8)
        ax.set_xlabel("loss % (each direction)")
        if logy: ax.set_yscale("log")
        ax.set_ylabel(ylabel)
    axes.flat[0].legend(frameon=False, loc="lower left")
    fig.suptitle(title, fontsize=13, x=0.01, ha="left", color="#111")
    fig.tight_layout(); p = os.path.join(outdir, fname); fig.savefig(p, dpi=110); plt.close(fig); print("wrote", p)

multiples(lambda t, d, l, c: val(t, d, l, c, "mbps"), "Mbit/s", "throughput_iperf3.png",
          "iperf3 throughput vs loss (single TCP stream, FrGW-02 -> 192.168.1.159)")
multiples(lambda t, d, l, c: val(t, d, l, c, "mbps"), "Mbit/s (log)", "throughput_iperf3_log.png",
          "iperf3 throughput vs loss - log scale", logy=True)
multiples(lambda t, d, l, c: val(t, d, l, c, "mbps"), "Mbit/s", "throughput_iperf2.png",
          "iperf2 throughput vs loss (single TCP stream)", tool="iperf2")
multiples(lambda t, d, l, c: retr_pct(t, d, l, c), "retransmitted %", "retrans_iperf3.png",
          "iperf3 retransmission rate (kernel TcpRetransSegs / TcpOutSegs)")
