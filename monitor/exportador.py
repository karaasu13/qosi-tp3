#!/usr/bin/env python3
"""
Exportador de métricas do TP3 para o Prometheus (base do Monitor).

Corre no host da VM, fora do caminho dos pacotes, e lê a cada pedido:
  - contadores das VNFs (firewall, regras do classifier e do policer, classes do scheduler);
  - fila e descartes do gargalo, nos dois sentidos;
  - pacotes por regra OpenFlow do ovs-sn (por onde o tráfego está a passar).

    sudo /opt/core/venv/bin/python monitor/exportador.py            # http://<vm>:9101/metrics
    sudo /opt/core/venv/bin/python monitor/exportador.py --uma-vez  # imprime e sai
"""

import argparse
import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "topologias"))

import contadores as cnt  # noqa: E402
import topologia as topo  # noqa: E402
from ovs_util import sh  # noqa: E402


def ex(vnf: str, c: str) -> str:
    r = subprocess.run(["docker", "exec", vnf, "sh", "-c", c], text=True, capture_output=True)
    return r.stdout if r.returncode == 0 else ""


class Metricas:
    def __init__(self):
        self.linhas, self.tipos = [], {}

    def add(self, nome, valor, tipo="counter", **lab):
        if nome not in self.tipos:
            self.tipos[nome] = tipo
            self.linhas.append(f"# TYPE {nome} {tipo}")
        l = ",".join(f'{k}="{v}"' for k, v in lab.items())
        self.linhas.append(f"{nome}{{{l}}} {valor}" if l else f"{nome} {valor}")

    def texto(self):
        return "\n".join(self.linhas) + "\n"


def recolher() -> str:
    m = Metricas()
    # firewall
    try:
        for k, v in cnt.firewall(ex).items():
            m.add("qosi_fw_pacotes_total", v, contador=k)
    except Exception:  # noqa: BLE001  (modo transparente: não há tabela)
        pass
    # classifier e policer
    for vnf in ("cl", "po"):
        for regra, v in cnt.regras(ex, vnf).items():
            m.add("qosi_regra_pacotes_total", v["pacotes"], vnf=vnf, regra=regra)
            m.add("qosi_regra_descartes_total", v["descartes"], vnf=vnf, regra=regra)
    # scheduler
    for classe, v in cnt.scheduler(ex).items():
        m.add("qosi_sc_bytes_total", v["bytes"], classe=classe)
        m.add("qosi_sc_pacotes_total", v["pacotes"], classe=classe)
        m.add("qosi_sc_descartes_total", v["descartes"], classe=classe)
        m.add("qosi_sc_fila_bytes", v["backlog_bytes"], "gauge", classe=classe)
    # gargalo
    for sentido, dev in zip(("descendente", "ascendente"), topo.gargalo_devs()):
        try:
            for q in json.loads(sh(f"tc -s -j qdisc show dev {dev}") or "[]"):
                if q.get("parent") == "1:1":
                    m.add("qosi_gargalo_bytes_total", q.get("bytes", 0), sentido=sentido)
                    m.add("qosi_gargalo_descartes_total", q.get("drops", 0), sentido=sentido)
                    m.add("qosi_gargalo_fila_bytes", q.get("backlog", 0), "gauge", sentido=sentido)
        except Exception:  # noqa: BLE001
            pass
    # OpenFlow: pacotes por porta de entrada do ovs-sn
    try:
        nome = {topo.porta("ovs-sn", i): k for k, i in topo.PORTA.items()}
        ofport = {int(sh(f"ovs-vsctl get Interface {d} ofport")): n for d, n in nome.items()}
        for ln in sh(f"ovs-ofctl dump-flows {topo.bridge('ovs-sn')}").splitlines():
            mp = re.search(r"n_packets=(\d+), n_bytes=(\d+).*in_port=(\d+)", ln)
            if mp:
                entrada = ofport.get(int(mp.group(3)), mp.group(3))
                m.add("qosi_ovs_pacotes_total", mp.group(1), entrada=entrada)
                m.add("qosi_ovs_bytes_total", mp.group(2), entrada=entrada)
    except Exception:  # noqa: BLE001
        pass
    return m.texto()


class Pedido(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("/metrics", ""):
            self.send_error(404)
            return
        corpo = recolher().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=9101)
    ap.add_argument("--uma-vez", action="store_true")
    a = ap.parse_args()
    if a.uma_vez:
        print(recolher(), end="")
        return
    print(f"exportador em http://0.0.0.0:{a.porta}/metrics")
    ThreadingHTTPServer(("0.0.0.0", a.porta), Pedido).serve_forever()


if __name__ == "__main__":
    main()
