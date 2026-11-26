#!/usr/bin/env python3
"""
E1 — teste funcional de cada VNF, com tráfego controlado (proposta, secção 7).

  fw  Firewall    clientes abrem ligações; o atacante não chega aos clientes
  cl  Classifier  cada classe chega ao cliente com o DSCP certo; DSCP dos hosts é apagado
  po  Policer     EF acima de 1 Mbit/s é cortado; vídeo acima de 5 Mbit/s passa a AF42;
                  bulk limitado a 3 Mbit/s por cliente
  sc  Scheduler   com o gargalo cheio, VoIP e vídeo protegidos e a fila sai do gargalo

Precisa da topologia ligada (make topologia). Põe as VNFs em modo cadeia e no
fim volta a pô-las em modo transparente.

    sudo /opt/core/venv/bin/python testes/e1.py            # todas
    sudo /opt/core/venv/bin/python testes/e1.py --vnf fw,cl
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "topologias"))
sys.path.insert(0, str(RAIZ / "testes"))

from core.api.grpc import client  # noqa: E402

import contadores as cnt  # noqa: E402
import topologia as topo  # noqa: E402
import trafego  # noqa: E402

IP = {**trafego.IP, "atacante": "10.1.9.66", "cli-web": "10.0.0.13"}
TMP = Path("/tmp/qosi_e1")


class Teste:
    def __init__(self, core):
        self.nos = trafego.Nos(core)
        self.core = core
        self.res = []

    # -------------------------------------------------------------- utilitários
    def ex(self, vnf, c):
        return self.nos.cmd(vnf, c)

    def ok(self, nome, cond, info=""):
        self.res.append((nome, bool(cond), info))

    def rc(self, no, c) -> int:
        rc, _ = self.core.node_command(topo.SESSAO, topo.N[no], c, wait=True, shell=True)
        return rc

    def capturar_tos(self, no: str, filtro: str, acao, n: int = 20) -> set:
        """Corre `acao()` enquanto captura em `no` e devolve os valores de TOS vistos."""
        f = TMP / f"cap_{no}.txt"
        f.unlink(missing_ok=True)
        self.nos.fundo(no, f"timeout 8 tcpdump -l -nn -v -c {n} -i eth0 {filtro} > {f} 2>&1")
        time.sleep(1.2)
        acao()
        time.sleep(1.5)
        txt = f.read_text() if f.exists() else ""
        # só o DSCP (ignora os 2 bits de ECN)
        return {hex(int(t, 16) & 0xFC) for t in re.findall(r"tos (0x[0-9a-f]+)", txt)}

    def servidores(self):
        trafego.preparar_servidores(self.nos, self._dir("srv"))

    def _dir(self, nome) -> Path:
        d = TMP / nome
        if d.exists():
            for p in sorted(d.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def contadores(self):
        return cnt.tudo(self.ex)

    # -------------------------------------------------------------- firewall
    def fw(self):
        antes = cnt.firewall(self.ex)
        ok_cli = self.rc("cli-web", f"ping -c 2 -W 2 {IP['srv-web']}") == 0
        self.ok("fw: cliente → servidor (ping) passa", ok_cli)
        ok_http = self.rc("cli-web", f"curl -s -m 5 -o /dev/null http://{IP['srv-web']}/obj.bin") == 0
        self.ok("fw: cliente → servidor (HTTP) passa", ok_http)
        bloq_ping = self.rc("atacante", f"ping -c 2 -W 2 {IP['cli-web']}") != 0
        self.ok("fw: atacante → cliente (ping) bloqueado", bloq_ping)
        self.nos.fundo("cli-web", f"python3 -m http.server 8080 --bind {IP['cli-web']}")
        time.sleep(1)
        bloq_tcp = self.rc("atacante", f"curl -s -m 3 -o /dev/null http://{IP['cli-web']}:8080/") != 0
        self.nos.matar("cli-web", "python3")
        self.ok("fw: atacante → cliente (TCP 8080) bloqueado", bloq_tcp)
        d = cnt.diferenca(cnt.firewall(self.ex), antes)
        self.ok("fw: contadores (novas, estabelecidas, bloqueados) a subir",
                d.get("novas_clientes", 0) > 0 and d.get("estabelecidas", 0) > 0
                and d.get("bloqueados", 0) > 0,
                ", ".join(f"{k}={v}" for k, v in d.items()))

    # -------------------------------------------------------------- classifier
    def cl(self):
        casos = [
            ("VoIP → EF (0xb8)", "cli-voip", f"udp and src host {IP['srv-voip']}", "0xb8",
             lambda: self.ex("cli-voip", f"irtt client -i 20ms -d 2s -q {IP['srv-voip']}:2112")),
            ("vídeo → AF41 (0x88)", "cli-video", "tcp and src port 5201 and greater 200", "0x88",
             lambda: self.ex("cli-video", f"iperf3 -c {IP['srv-video']} -p 5201 -R -b 1M -t 2")),
            ("web → BE (0x0)", "cli-web", "tcp and src port 80 and greater 200", "0x0",
             lambda: self.ex("cli-web", f"curl -s -o /dev/null http://{IP['srv-web']}/obj.bin")),
            ("bulk → LE (0x4)", "cli-bulk1", "tcp and src port 5202 and greater 200", "0x4",
             lambda: self.ex("cli-bulk1", f"iperf3 -c {IP['srv-web']} -p 5202 -R -t 2")),
        ]
        for nome, no, filtro, tos, acao in casos:
            vistos = self.capturar_tos(no, filtro, acao)
            self.ok(f"cl: {nome}", vistos == {tos}, f"visto: {', '.join(sorted(vistos)) or 'nada'}")
        # fronteira de confiança: o cliente marca EF, o servidor tem de receber BE
        envio = ("python3 -c \"import socket;s=socket.socket(2,2);s.setsockopt(0,1,0xb8);"
                 f"[s.sendto(b'x'*200,('{IP['srv-web']}',9999)) for _ in range(10)]\"")
        vistos = self.capturar_tos("srv-web", "udp and dst port 9999",
                                   lambda: self.ex("cli-web", envio), n=10)
        self.ok("cl: fronteira de confiança (EF do cliente → BE)", vistos == {"0x0"},
                f"visto: {', '.join(sorted(vistos)) or 'nada'}")

    # -------------------------------------------------------------- policer
    def po(self):
        d = self._dir("po")
        # EF a ~4 Mbit/s nos dois sentidos (irtt a cada 1 ms com 500 B)
        antes = cnt.regras(self.ex, "po")
        self.ex("cli-voip", f"irtt client -i 1ms -l 500 -d 3s -q -o {d}/ef.json "
                            f"{IP['srv-voip']}:2112")
        perdas = json.loads((d / "ef.json").read_text())["stats"]["downstream_loss_percent"]
        dif = cnt.diferenca(cnt.regras(self.ex, "po"), antes)
        self.ok("po: EF acima de 1 Mbit/s é descartado", dif["ef"]["descartes"] > 0 and perdas > 30,
                f"descartados no policer: {dif['ef']['descartes']}, perdas VoIP: {perdas:.0f}%")
        # vídeo a 8 Mbit/s: o excesso tem de aparecer como AF42
        antes = cnt.regras(self.ex, "po")
        vistos = self.capturar_tos(
            "cli-video", "tcp and src port 5201 and greater 200",
            lambda: self.ex("cli-video", f"iperf3 -c {IP['srv-video']} -p 5201 -R -b 8M -t 4"),
            n=400)
        dif = cnt.diferenca(cnt.regras(self.ex, "po"), antes)
        exc = dif["af41_excesso_af42"]["pacotes"]
        self.ok("po: vídeo acima de 5 Mbit/s remarcado AF42", exc > 0 and "0x90" in vistos,
                f"{exc} pacotes remarcados; DSCP no cliente: {', '.join(sorted(vistos))}")
        # bulk: um cliente sozinho não passa de ~3 Mbit/s
        self.ex("cli-bulk1", f"iperf3 -c {IP['srv-web']} -p 5202 -R -t 5 -J > {d}/bulk.json")
        mbps, _ = trafego.mbps_iperf(d / "bulk.json")
        self.ok("po: bulk limitado a ~3 Mbit/s por cliente", 1.5 < mbps < 3.4, f"{mbps:.2f} Mbit/s")

    # -------------------------------------------------------------- scheduler
    def sc(self):
        antes = cnt.scheduler(self.ex)
        m = trafego.correr(self.nos, "C2", 15, self._dir("sc_c2"))
        dif = cnt.diferenca(cnt.scheduler(self.ex), antes)
        self.ok("sc: cada classe na sua fila HTB",
                all(dif.get(c, {}).get("pacotes", 0) > 0 for c in ("ef", "af4", "be", "le")),
                " ".join(f"{c}:{dif.get(c, {}).get('pacotes', 0)}" for c in ("ef", "af4", "be", "le")))
        self.ok("sc: VoIP protegido com o gargalo cheio (p95 < 30 ms)",
                m["voip_atraso_p95_ms"] < 30, f"p95 {m['voip_atraso_p95_ms']:.1f} ms")
        self.ok("sc: vídeo recebe o que pede (>= 3,5 de 4 Mbit/s)", m["video_mbps"] >= 3.5,
                f"{m['video_mbps']:.2f} Mbit/s")
        self.ok("sc: a fila sai do gargalo (máx. < 15 ms)", m["fila_max_ms"] < 15,
                f"fila máx. no gargalo {m['fila_max_ms']:.1f} ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vnf", default="fw,cl,po,sc")
    a = ap.parse_args()
    core = client.CoreGrpcClient()
    core.connect()
    if not topo.sessao_completa(core):
        print("A topologia não está ligada (ou ficou incompleta): corre 'make topologia'.")
        return 1
    t = Teste(core)
    TMP.mkdir(exist_ok=True)
    topo.configurar_gargalo("C0")
    try:
        topo.aplicar_modo(core, "cadeia")
        t.servidores()
        for v in a.vnf.split(","):
            print(f"a testar {v} ...", flush=True)
            try:
                getattr(t, v.strip())()
            except Exception as e:  # noqa: BLE001
                t.ok(f"{v}: erro a correr o teste", False, str(e)[:200])
    finally:
        topo.aplicar_modo(core, "transparente")
    print()
    for nome, ok, info in t.res:
        print(f"  [{'OK' if ok else 'FALHOU':^6}] {nome}" + (f"  ({info})" if info else ""))
    tudo = all(ok for _, ok, _ in t.res)
    print("\nE1:", "OK" if tudo else "FALHOU")
    return 0 if tudo else 1


if __name__ == "__main__":
    sys.exit(main())
