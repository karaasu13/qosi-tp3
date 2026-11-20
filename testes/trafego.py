#!/usr/bin/env python3
"""
Cenário de tráfego do TP3: as 4 classes ao mesmo tempo, a congestionar o gargalo.

  VoIP   srv-voip  → cli-voip    irtt, 20 ms / 172 B (G.711): atraso num sentido, jitter, perdas
  Vídeo  srv-video → cli-video   iperf3 TCP limitado a 4 Mbit/s: débito, retransmissões
  Web    srv-web   → cli-web     pedidos HTTP de 100 KB (curl): tempo de resposta
  Bulk   srv-web   → cli-bulk1-3 iperf3 TCP, 2 fluxos por cliente: débito e equidade (Jain)

Tudo no sentido descendente (servidores → clientes), que é o que passa no gargalo.
Os nós partilham o relógio da VM, por isso o atraso num só sentido do irtt é exato.

Precisa da topologia ligada (make topologia). Uso, na VM:
    sudo /opt/core/venv/bin/python testes/trafego.py --config C0
    sudo /opt/core/venv/bin/python testes/trafego.py --config C0,C1 --duracao 60 --repeticoes 5
"""

import argparse
import csv
import json
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "topologias"))

from core.api.grpc import client  # noqa: E402

import topologia as topo  # noqa: E402
from ovs_util import sh  # noqa: E402

IP = {"srv-voip": "10.1.1.10", "srv-video": "10.1.2.10", "srv-web": "10.1.3.10"}
BULK = ["cli-bulk1", "cli-bulk2", "cli-bulk3"]
WEB_OBJ_KB = 100
DESCRICAO = {
    "C0": "sem QoS, FIFO longa (150 ms)",
    "C1": "AQM (fq_codel) no gargalo",
}


# ------------------------------------------------------------------ nós
class Nos:
    def __init__(self, core):
        self.core = core

    def cmd(self, no: str, c: str, wait: bool = True) -> str:
        rc, out = self.core.node_command(topo.SESSAO, topo.N[no], c, wait=wait, shell=True)
        if wait and rc:
            raise RuntimeError(f"{no}: {c}: {out.strip()[-300:]}")
        return out

    def fundo(self, no: str, c: str) -> None:
        """Arranca em segundo plano dentro do nó."""
        self.cmd(no, f"nohup sh -c '{c}' >/dev/null 2>&1 &", wait=False)

    def matar(self, no: str, prog: str) -> None:
        self.core.node_command(topo.SESSAO, topo.N[no], f"pkill -x {prog}", wait=True)


# ------------------------------------------------------------------ gargalo
class AmostradorFila(threading.Thread):
    """Lê a fila do gargalo descendente a cada 0,2 s (backlog e descartes)."""

    def __init__(self, dev: str):
        super().__init__(daemon=True)
        self.dev, self.amostras, self.parar = dev, [], threading.Event()

    def ler(self):
        for q in json.loads(sh(f"tc -s -j qdisc show dev {self.dev}") or "[]"):
            if q.get("parent") == "1:1":  # a folha (bfifo / fq_codel)
                return q.get("backlog", 0), q.get("qlen", 0), q.get("drops", 0)
        return 0, 0, 0

    def run(self):
        t0 = time.time()
        while not self.parar.is_set():
            b, n, d = self.ler()
            self.amostras.append((round(time.time() - t0, 2), b, n, d))
            time.sleep(0.2)


# ------------------------------------------------------------------ execução
def preparar_servidores(nos: Nos, d: Path) -> None:
    for no, progs in {"srv-voip": ["irtt"], "srv-video": ["iperf3"],
                      "srv-web": ["iperf3", "python3"]}.items():
        for p in progs:
            nos.matar(no, p)
    web = d / "web"
    web.mkdir()
    (web / "obj.bin").write_bytes(b"\0" * WEB_OBJ_KB * 1024)
    nos.fundo("srv-voip", f"irtt server -b {IP['srv-voip']}:2112")
    nos.cmd("srv-video", "iperf3 -s -p 5201 -D")
    for k in range(3):
        nos.cmd("srv-web", f"iperf3 -s -p {5202 + k} -D")
    nos.fundo("srv-web", f"cd {web} && python3 -m http.server 80 --bind {IP['srv-web']}")
    time.sleep(1.5)


def correr(nos: Nos, config: str, duracao: int, d: Path) -> dict:
    topo.configurar_gargalo(config)
    preparar_servidores(nos, d)
    fila = AmostradorFila(topo.gargalo_devs()[0])
    fila.start()

    # bulk primeiro, para o gargalo já estar cheio quando as outras classes começam
    for k, cli in enumerate(BULK):
        nos.fundo(cli, f"iperf3 -c {IP['srv-web']} -p {5202 + k} -R -P 2 "
                       f"-t {duracao + 6} -J > {d}/bulk{k + 1}.json")
    time.sleep(3)
    nos.fundo("cli-video", f"iperf3 -c {IP['srv-video']} -p 5201 -R -b 4M "
                           f"-t {duracao} -J > {d}/video.json")
    nos.fundo("cli-voip", f"irtt client -i 20ms -l 172 -d {duracao}s -q "
                          f"-o {d}/voip.json {IP['srv-voip']}:2112")
    web_sh = d / "web_cliente.sh"
    web_sh.write_text(
        "#!/bin/bash\n"
        f"fim=$((SECONDS + {duracao}))\n"
        "while [ $SECONDS -lt $fim ]; do\n"
        f"  curl -s -o /dev/null -m 10 -w '%{{time_total}} %{{http_code}}\\n' "
        f"http://{IP['srv-web']}/obj.bin\n"
        "  sleep 0.5\n"
        "done\n")
    nos.fundo("cli-web", f"bash {web_sh} > {d}/web.txt")

    print(f"  [{config}] a medir durante {duracao} s ...", flush=True)
    time.sleep(duracao + 8)
    fila.parar.set()
    fila.join()
    for no, p in [("srv-voip", "irtt"), ("srv-video", "iperf3"), ("srv-web", "iperf3"),
                  ("srv-web", "python3")]:
        nos.matar(no, p)

    with open(d / "fila.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "backlog_bytes", "backlog_pkts", "drops_acum"])
        w.writerows(fila.amostras)
    return metricas(d, fila.amostras)


# ------------------------------------------------------------------ métricas
def pct(v: list, p: float) -> float:
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def mbps_iperf(f: Path) -> tuple:
    j = json.loads(f.read_text())
    if "error" in j:
        raise RuntimeError(f"{f.name}: {j['error']}")
    e = j["end"]
    return e["sum_received"]["bits_per_second"] / 1e6, e.get("sum_sent", {}).get("retransmits", 0)


def metricas(d: Path, fila: list) -> dict:
    m = {}
    # VoIP
    j = json.loads((d / "voip.json").read_text())
    rts = j["round_trips"]
    owd = [r["delay"]["receive"] / 1e6 for r in rts if r["lost"] == "false"]
    ipdv = [abs(r["ipdv"]["receive"]) / 1e6 for r in rts if r.get("ipdv", {}).get("receive") is not None]
    m["voip_atraso_p50_ms"] = pct(owd, 50)
    m["voip_atraso_p95_ms"] = pct(owd, 95)
    m["voip_atraso_p99_ms"] = pct(owd, 99)
    m["voip_jitter_ms"] = statistics.mean(ipdv) if ipdv else float("nan")
    m["voip_perdas_pct"] = j["stats"]["downstream_loss_percent"]
    # Vídeo
    m["video_mbps"], m["video_retrans"] = mbps_iperf(d / "video.json")
    # Web
    linhas = [ln.split() for ln in (d / "web.txt").read_text().split("\n") if ln.strip()]
    ok = [float(t) * 1000 for t, code in linhas if code == "200"]
    m["web_pedidos"] = len(ok)
    m["web_falhas"] = len(linhas) - len(ok)
    m["web_resposta_p50_ms"] = pct(ok, 50)
    m["web_resposta_p95_ms"] = pct(ok, 95)
    # Bulk
    bulk = [mbps_iperf(d / f"bulk{k}.json")[0] for k in (1, 2, 3)]
    m["bulk_mbps_total"] = sum(bulk)
    m["bulk_jain"] = sum(bulk) ** 2 / (len(bulk) * sum(x * x for x in bulk)) if any(bulk) else 0
    # Gargalo
    if fila:
        bmax = max(a[1] for a in fila)
        m["fila_max_ms"] = bmax * 8 / (topo.GARGALO_KBIT * 1000) * 1000
        m["fila_media_ms"] = statistics.mean(a[1] for a in fila) * 8 / (topo.GARGALO_KBIT * 1000) * 1000
        m["fila_descartes"] = fila[-1][3] - fila[0][3]
    return m


# ------------------------------------------------------------------ saída
LINHAS = [
    ("VoIP atraso p50 (ms)", "voip_atraso_p50_ms", 1),
    ("VoIP atraso p95 (ms)", "voip_atraso_p95_ms", 1),
    ("VoIP atraso p99 (ms)", "voip_atraso_p99_ms", 1),
    ("VoIP jitter (ms)", "voip_jitter_ms", 2),
    ("VoIP perdas (%)", "voip_perdas_pct", 2),
    ("Vídeo débito (Mbit/s)", "video_mbps", 2),
    ("Vídeo retransmissões", "video_retrans", 0),
    ("Web resposta p50 (ms)", "web_resposta_p50_ms", 0),
    ("Web resposta p95 (ms)", "web_resposta_p95_ms", 0),
    ("Web pedidos OK / falhas", None, None),
    ("Bulk débito total (Mbit/s)", "bulk_mbps_total", 2),
    ("Bulk equidade (Jain)", "bulk_jain", 3),
    ("Fila gargalo média (ms)", "fila_media_ms", 1),
    ("Fila gargalo máx. (ms)", "fila_max_ms", 1),
    ("Descartes no gargalo", "fila_descartes", 0),
]


def media(runs: list, k: str) -> float:
    v = [r[k] for r in runs if k in r and r[k] == r[k]]  # ignora NaN
    return statistics.mean(v) if v else float("nan")


def tabela(res: dict) -> str:
    cfgs = list(res)
    larg = 28
    out = [f"{'':{larg}}" + "".join(f"{c:>14}" for c in cfgs)]
    for nome, k, casas in LINHAS:
        if k is None:
            vals = [f"{media(res[c], 'web_pedidos'):.0f} / {media(res[c], 'web_falhas'):.0f}"
                    for c in cfgs]
        else:
            vals = [f"{media(res[c], k):.{casas}f}" for c in cfgs]
        out.append(f"{nome:{larg}}" + "".join(f"{v:>14}" for v in vals))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default="C0", help="C0, C1 ou lista (C0,C1)")
    ap.add_argument("--duracao", type=int, default=30, help="segundos de medição")
    ap.add_argument("--repeticoes", type=int, default=1)
    a = ap.parse_args()
    configs = [c.strip().upper() for c in a.config.split(",")]
    for c in configs:
        if c not in DESCRICAO:
            ap.error(f"configuração desconhecida: {c} (por agora: {', '.join(DESCRICAO)})")

    core = client.CoreGrpcClient()
    core.connect()
    if not any(s.id == topo.SESSAO for s in core.get_sessions()):
        print("A topologia não está ligada: corre primeiro 'make topologia'.")
        return 1
    nos = Nos(core)

    base = RAIZ / "resultados" / datetime.now().strftime("%Y%m%d-%H%M%S")
    res = {c: [] for c in configs}
    for c in configs:
        print(f"{c}: {DESCRICAO[c]}")
        for r in range(1, a.repeticoes + 1):
            d = base / f"{c}_r{r}"
            d.mkdir(parents=True)
            m = correr(nos, c, a.duracao, d)
            (d / "metricas.json").write_text(json.dumps(m, indent=2))
            res[c].append(m)
    topo.configurar_gargalo("C0")  # deixa a topologia como estava

    # CSV com uma linha por execução
    chaves = sorted({k for runs in res.values() for m in runs for k in m})
    with open(base / "resumo.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["config", "repeticao"] + chaves)
        for c, runs in res.items():
            for r, m in enumerate(runs, 1):
                w.writerow([c, r] + [m.get(k, "") for k in chaves])

    print(f"\nMédia de {a.repeticoes} execução(ões) de {a.duracao} s:\n")
    print(tabela(res))
    print(f"\nResultados em {base.relative_to(RAIZ)}/ (resumo.csv, fila.csv e JSON de cada ferramenta)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
