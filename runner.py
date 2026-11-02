#!/usr/bin/env python3
"""CCA comparison under netem loss/delay on FrGW-02 (egress br0 + ingress via ifb0)."""
import json, os, re, subprocess, time, datetime

SERVER = "192.168.1.159"
CCAS = ["cubic", "bbr", "westwood"]
LOSSES = [0.0, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.2, 0.4, 0.6, 0.8, 1, 2, 4, 6, 8, 10]
DELAYS = [0, 1, 2, 3]
DUR = 10
OUT = "/root/cca_test"
IFACES = ["br0", "enp1s0f0", "enp1s0f1"]
LIMIT = 100000

def sh(cmd, check=True, timeout=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr}")
    return r

def setup():
    sh("modprobe tcp_westwood; modprobe tcp_bbr; modprobe ifb numifbs=0; modprobe sch_netem")
    sh("ip link del ifb0", check=False)
    sh("ip link add ifb0 type ifb && ip link set ifb0 up")
    for i in IFACES:
        sh(f"ethtool -K {i} tso off gso off gro off", check=False)
    # TCP always builds GSO skbs and segments after the qdisc; force 1 MSS per skb so
    # netem loss is applied per packet rather than per ~64KB super-packet.
    sh("ip link set dev br0 gso_max_segs 1")
    sh("tc qdisc del dev br0 root", check=False)
    sh("tc qdisc del dev br0 ingress", check=False)
    sh("tc qdisc add dev br0 root handle 1: prio bands 3 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
    sh(f"tc qdisc add dev br0 parent 1:3 handle 30: netem limit {LIMIT} delay 0ms loss 0%")
    sh(f"tc filter add dev br0 parent 1: protocol ip prio 1 u32 match ip dst {SERVER}/32 flowid 1:3")
    sh("tc qdisc add dev br0 handle ffff: ingress")
    sh(f"tc filter add dev br0 parent ffff: protocol ip prio 1 u32 match ip src {SERVER}/32 "
       "action mirred egress redirect dev ifb0")
    sh(f"tc qdisc add dev ifb0 root handle 1: netem limit {LIMIT} delay 0ms loss 0%")

def teardown():
    sh("tc qdisc del dev br0 root", check=False)
    sh("tc qdisc del dev br0 ingress", check=False)
    sh("ip link del ifb0", check=False)
    for i in IFACES:
        sh(f"ethtool -K {i} tso on gso on gro on", check=False)
    sh("ip link set dev br0 gso_max_segs 65535", check=False)

def set_netem(delay, loss):
    sh(f"tc qdisc change dev br0 parent 1:3 handle 30: netem limit {LIMIT} delay {delay}ms loss {loss}%")
    sh(f"tc qdisc change dev ifb0 root handle 1: netem limit {LIMIT} delay {delay}ms loss {loss}%")

def netem_counters():
    res = {}
    for name, cmd in (("egress", "tc -s qdisc show dev br0 parent 1:3"),
                      ("ingress", "tc -s qdisc show dev ifb0 root")):
        out = sh(cmd).stdout
        m = re.search(r"Sent \d+ bytes (\d+) pkt \(dropped (\d+)", out)
        res[name] = (int(m.group(1)), int(m.group(2)))
    return res

def loss_delta(a, b):
    d = {}
    for k in a:
        sent = b[k][0] - a[k][0]; drop = b[k][1] - a[k][1]
        d[k + "_pkts"] = sent + drop
        d[k + "_dropped"] = drop
        d[k + "_loss_pct"] = round(100.0 * drop / (sent + drop), 4) if sent + drop else None
    return d

def nstat():
    out = sh("nstat -az TcpOutSegs TcpRetransSegs").stdout
    return {k: int(v) for k, v in re.findall(r"^(Tcp\w+)\s+(\d+)", out, re.M)}

def run_iperf3(cca, tag):
    for attempt in range(3):
        r = sh(f"timeout {DUR + 30} iperf3 -c {SERVER} -t {DUR} -C {cca} -J --connect-timeout 5000",
               check=False)
        open(f"{OUT}/raw/{tag}_iperf3_try{attempt}.json", "w").write(r.stdout + r.stderr)
        try:
            j = json.loads(r.stdout)
            if "error" in j:
                raise ValueError(j["error"])
            s = j["end"]["streams"][0]["sender"]
            return {"ok": True, "mbps": j["end"]["sum_sent"]["bits_per_second"] / 1e6,
                    "recv_mbps": j["end"]["sum_received"]["bits_per_second"] / 1e6,
                    "retransmits": j["end"]["sum_sent"]["retransmits"],
                    "mean_rtt_us": s.get("mean_rtt"), "min_rtt_us": s.get("min_rtt"),
                    "max_rtt_us": s.get("max_rtt"), "max_cwnd_bytes": s.get("max_snd_cwnd"),
                    "cca_reported": j["end"].get("sender_tcp_congestion"),
                    "bytes": j["end"]["sum_sent"]["bytes"], "attempts": attempt + 1}
        except Exception as e:
            err = str(e)
            time.sleep(3)
    return {"ok": False, "error": err}

IPERF2_RE = re.compile(r"\[\s*\d+\]\s+0\.0+-\s*([\d.]+) sec\s+([\d.]+) (\w)Bytes\s+([\d.]+) Mbits/sec\s+"
                       r"(\d+)/(\d+)\s+(\d+)\s+(\S+)/(\S+)/(\d+)\((\d+)\) us")

def run_iperf2(cca, tag):
    for attempt in range(3):
        r = sh(f"timeout {DUR + 30} iperf -c {SERVER} -t {DUR} -Z {cca} -e -f m", check=False)
        open(f"{OUT}/raw/{tag}_iperf2_try{attempt}.txt", "w").write(r.stdout + r.stderr)
        m = None
        for line in r.stdout.splitlines():
            mm = IPERF2_RE.search(line)
            if mm:
                m = mm
        if m and f"({cca})" in r.stdout:
            return {"ok": True, "mbps": float(m.group(4)), "retransmits": int(m.group(7)),
                    "cwnd_field": m.group(9), "rtt_us": int(m.group(10)), "attempts": attempt + 1}
        time.sleep(3)
    return {"ok": False, "error": (r.stdout + r.stderr)[-300:]}

def main():
    os.makedirs(f"{OUT}/raw", exist_ok=True)
    resf = open(f"{OUT}/results.jsonl", "a")
    done = set()
    if os.path.exists(f"{OUT}/results.jsonl"):
        for l in open(f"{OUT}/results.jsonl"):
            j = json.loads(l); done.add((j["tool"], j["delay_ms"], j["loss_pct"], j["cca"]))
    setup()
    try:
        total = len(DELAYS) * len(LOSSES) * len(CCAS) * 2; n = 0
        for delay in DELAYS:
            for loss in LOSSES:
                set_netem(delay, loss)
                time.sleep(1)
                for cca in CCAS:
                    for tool, fn in (("iperf3", run_iperf3), ("iperf2", run_iperf2)):
                        n += 1
                        if (tool, delay, loss, cca) in done:
                            continue
                        tag = f"d{delay}_l{loss}_{cca}"
                        c0 = netem_counters(); n0 = nstat()
                        t0 = datetime.datetime.now().isoformat(timespec="seconds")
                        res = fn(cca, tag)
                        c1 = netem_counters(); n1 = nstat()
                        res["kernel_out_segs"] = n1["TcpOutSegs"] - n0["TcpOutSegs"]
                        res["kernel_retrans_segs"] = n1["TcpRetransSegs"] - n0["TcpRetransSegs"]
                        rec = {"tool": tool, "delay_ms": delay, "loss_pct": loss, "cca": cca,
                               "start": t0, **res, **loss_delta(c0, c1)}
                        resf.write(json.dumps(rec) + "\n"); resf.flush()
                        print(f"[{n}/{total}] {tool} d={delay} l={loss} {cca}: "
                              f"{res.get('mbps', 'FAIL')}", flush=True)
                        time.sleep(1)
    finally:
        teardown()
        print("DONE", flush=True)

if __name__ == "__main__":
    main()
