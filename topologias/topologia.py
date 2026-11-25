#!/usr/bin/env python3
"""
Topologia completa do TP3 (figura da proposta), criada no core-daemon.

  srv-voip  ─┐                                 ┌─ cli-voip
  srv-video ─┤                 nó de serviço   ├─ cli-video
  srv-web   ─┼─ r1 ── r2 ── [ovs-sn: fw→cl→po→sc] ══ sw-acesso ┼─ cli-web
  atacante  ─┘   core   borda             gargalo 10 Mbit/s    └─ cli-bulk1..3

A sessão fica a correr no core-daemon e aparece no core-gui
(Session → Sessions → sessão 10). As VNFs ainda estão em modo transparente:
as funções (nftables, tc, HTB...) entram por cima disto, VNF a VNF.

Sentido descendente (servidores → clientes): r2 → fw → cl → po → sc → gargalo.
Sentido ascendente: gargalo → cl → fw → r2 (sem policiamento nem agendamento).

Uso (na VM, com o core-daemon ativo):
    sudo /opt/core/venv/bin/python topologias/topologia.py           # cria, configura e testa
    sudo /opt/core/venv/bin/python topologias/topologia.py --parar   # apaga a sessão
"""

import argparse
import re
import sys
from pathlib import Path
import time

from core.api.grpc import client
from core.api.grpc.wrappers import Interface, NodeType, Position

from ovs_util import desligar_ipv6_host, limpar_macs, ptp_como_fios, sh, short_id

SESSAO = 10
IMAGEM_VNF = "qosi/vnf:latest"
GARGALO_KBIT = 10_000
GARGALO_BUFFER_MS = 150  # FIFO longa do C0 (bufferbloat)

# ids dos nós (fixos, para os nomes das bridges e portas serem previsíveis)
N = {
    "srv-voip": 1, "srv-video": 2, "srv-web": 3, "atacante": 4,
    "r1": 5, "r2": 6, "ovs-sn": 7,
    "fw": 8, "cl": 9, "po": 10, "sc": 11,
    "sw-acesso": 12,
    "cli-voip": 13, "cli-video": 14, "cli-web": 15,
    "cli-bulk1": 16, "cli-bulk2": 17, "cli-bulk3": 18,
}
SERVIDORES = {  # nome: (sub-rede, ip do servidor, iface de r1)
    "srv-voip": ("10.1.1", 10, 0), "srv-video": ("10.1.2", 10, 1),
    "srv-web": ("10.1.3", 10, 2), "atacante": ("10.1.9", 66, 3),
}
CLIENTES = {"cli-voip": 11, "cli-video": 12, "cli-web": 13,
            "cli-bulk1": 21, "cli-bulk2": 22, "cli-bulk3": 23}
VNFS = ["fw", "cl", "po", "sc"]
# portas do switch do nó de serviço: id da interface no ovs-sn
PORTA = {"r2": 0, "fw0": 1, "fw1": 2, "cl0": 3, "cl1": 4,
         "po0": 5, "po1": 6, "sc0": 7, "sc1": 8, "acesso": 9}


def iface(i: int, ip: str = None, mask: int = 24, nome: str = None) -> Interface:
    return Interface(id=i, name=nome or f"eth{i}", ip4=ip, ip4_mask=mask if ip else None)


def construir(core: client.CoreGrpcClient):
    if any(s.id == SESSAO for s in core.get_sessions()):
        core.delete_session(SESSAO)
    s = core.create_session(SESSAO)
    s.set_options({"ovs": "1"})

    def pc(nome, x, y):
        n = s.add_node(N[nome], name=nome, model="PC", position=Position(x=x, y=y))
        n.services = {"DefaultRoute"}  # rota por omissão via .1 da sub-rede
        return n

    def router(nome, x, y):
        n = s.add_node(N[nome], name=nome, model="router", position=Position(x=x, y=y))
        n.services = {"IPForward"}  # rotas estáticas aplicadas depois do arranque
        return n

    nos = {}
    for k, nome in enumerate(SERVIDORES):
        nos[nome] = pc(nome, 80, 120 + k * 110)
    nos["r1"] = router("r1", 250, 285)
    nos["r2"] = router("r2", 390, 285)
    nos["ovs-sn"] = s.add_node(N["ovs-sn"], name="ovs-sn", _type=NodeType.SWITCH,
                               position=Position(x=640, y=285))
    for k, nome in enumerate(VNFS):
        nos[nome] = s.add_node(N[nome], name=nome, _type=NodeType.DOCKER, model="",
                               image=IMAGEM_VNF, position=Position(x=520 + k * 80, y=160))
    nos["sw-acesso"] = s.add_node(N["sw-acesso"], name="sw-acesso",
                                  _type=NodeType.SWITCH, position=Position(x=880, y=285))
    for k, nome in enumerate(CLIENTES):
        nos[nome] = pc(nome, 1040, 60 + k * 90)

    # servidores e atacante → r1 (r1 é o .1 de cada sub-rede)
    for nome, (rede, host, r1_if) in SERVIDORES.items():
        s.add_link(node1=nos[nome], node2=nos["r1"],
                   iface1=iface(0, f"{rede}.{host}"), iface2=iface(r1_if, f"{rede}.1"))
    s.add_link(node1=nos["r1"], node2=nos["r2"],
               iface1=iface(4, "10.255.0.1", 30), iface2=iface(0, "10.255.0.2", 30))
    # r2 (10.0.0.1, gateway dos clientes) → nó de serviço
    s.add_link(node1=nos["r2"], node2=nos["ovs-sn"],
               iface1=iface(1, "10.0.0.1"), iface2=iface(PORTA["r2"]))
    # cada VNF liga ao ovs-sn por duas interfaces, sem IP (transparente em L2)
    for v in VNFS:
        s.add_link(node1=nos[v], node2=nos["ovs-sn"], iface1=iface(0),
                   iface2=iface(PORTA[f"{v}0"]))
        s.add_link(node1=nos[v], node2=nos["ovs-sn"], iface1=iface(1),
                   iface2=iface(PORTA[f"{v}1"]))
    # gargalo: ovs-sn ↔ sw-acesso
    s.add_link(node1=nos["ovs-sn"], node2=nos["sw-acesso"],
               iface1=iface(PORTA["acesso"]), iface2=iface(0))
    for k, (nome, host) in enumerate(CLIENTES.items(), start=1):
        s.add_link(node1=nos[nome], node2=nos["sw-acesso"],
                   iface1=iface(0, f"10.0.0.{host}"), iface2=iface(k))

    core.start_session(s)
    return s


def cmd(core, nome: str, c: str) -> str:
    rc, out = core.node_command(SESSAO, N[nome], c, wait=True, shell=True)
    if rc:
        raise RuntimeError(f"{nome}: {c}: {out.strip()}")
    return out


def bridge(nome: str) -> str:
    return f"b.{N[nome]}.{short_id(SESSAO)}"


def porta(sw: str, iface_id: int) -> str:
    return f"veth{N[sw]}.{iface_id}.{short_id(SESSAO)}"


def ofport(dev: str) -> int:
    return int(sh(f"ovs-vsctl get Interface {dev} ofport"))


def regras_cadeia() -> dict:
    br = bridge("ovs-sn")
    p = {k: ofport(porta("ovs-sn", i)) for k, i in PORTA.items()}
    sh(f"ovs-vsctl set-fail-mode {br} secure")
    sh(f"ovs-ofctl del-flows {br}")
    flows = ["priority=0,actions=drop"]
    desc = ["r2", "fw0", "fw1", "cl0", "cl1", "po0", "po1", "sc0", "sc1", "acesso"]
    # descendente: r2→fw0, fw1→cl0, cl1→po0, po1→sc0, sc1→acesso
    for a, b in zip(desc[0::2], desc[1::2]):
        flows.append(f"priority=100,in_port={p[a]},actions=output:{p[b]}")
    # ascendente: acesso→cl1, cl0→fw1, fw0→r2 (não passa no policer nem no scheduler)
    for a, b in [("acesso", "cl1"), ("cl0", "fw1"), ("fw0", "r2")]:
        flows.append(f"priority=100,in_port={p[a]},actions=output:{p[b]}")
    for f in flows:
        sh(f"ovs-ofctl add-flow {br} '{f}'")
    return p


def gargalo_devs() -> list:
    """Interfaces do gargalo: [descendente (ovs-sn → acesso), ascendente (sw-acesso → ovs-sn)]."""
    return [porta("ovs-sn", PORTA["acesso"]), porta("sw-acesso", 0)]


def configurar_gargalo(modo: str = "C0", kbit: int = GARGALO_KBIT,
                       buffer_ms: int = GARGALO_BUFFER_MS) -> None:
    """Gargalo de `kbit` nos dois sentidos: HTB a limitar o débito e, por baixo,
    a disciplina de fila que muda entre configurações.

    C0: FIFO longa (bfifo de `buffer_ms`), sem QoS  -> bufferbloat
    C1: fq_codel (AQM) no lugar da FIFO
    """
    limite = int(kbit * 1000 / 8 * buffer_ms / 1000)  # bytes
    folha = {"C0": f"bfifo limit {limite}", "C1": "fq_codel"}[modo]
    for dev in gargalo_devs():
        sh(f"ethtool -K {dev} gro off gso off tso off", check=False)
        sh(f"tc qdisc del dev {dev} root", check=False)
        sh(f"tc qdisc add dev {dev} root handle 1: htb default 1")
        sh(f"tc class add dev {dev} parent 1: classid 1:1 htb rate {kbit}kbit "
           f"ceil {kbit}kbit burst 15k cburst 15k")
        sh(f"tc qdisc add dev {dev} parent 1:1 handle 10: {folha}")


def configurar(core):
    # rotas estáticas nos routers
    cmd(core, "r1", "ip route replace 10.0.0.0/24 via 10.255.0.2")
    cmd(core, "r2", "ip route replace default via 10.255.0.1")
    # host: sem IPv6 nas interfaces do CORE e bridges ptp como fios (ver ovs_util)
    desligar_ipv6_host(SESSAO)
    ptp_como_fios(SESSAO)
    # regras OpenFlow ANTES de ligar as VNFs (evita ciclos L2 no ovs-sn)
    p = regras_cadeia()
    aplicar_modo(core, "transparente")
    configurar_gargalo()
    # esquece MACs aprendidos durante o arranque, antes de haver cadeia
    limpar_macs(bridge("sw-acesso"))
    return p


# módulos do kernel de que as VNFs precisam (os contentores não os podem carregar)
MODULOS = ["nf_conntrack_bridge", "br_netfilter", "sch_htb", "sch_fq_codel", "cls_flower",
           "cls_u32", "act_mirred", "act_pedit", "act_csum", "act_police", "act_gact"]
SCRIPT_VNF = {"fw": "vnf-firewall", "cl": "vnf-classifier",
              "po": "vnf-policer", "sc": "vnf-scheduler"}


def aplicar_modo(core, modo: str) -> None:
    """transparente: as VNFs só reencaminham (C0, C1).
    cadeia: cada VNF faz a sua função (C2)."""
    if modo == "cadeia":
        for m in MODULOS:
            sh(f"modprobe {m}", check=False)
    # copia os scripts atuais para dentro das VNFs (evita recriar a topologia
    # sempre que uma VNF muda)
    bin_vnf = Path(__file__).resolve().parent.parent / "vnf" / "bin"
    for v in VNFS:
        sh(f"docker cp {bin_vnf}/. {v}:/usr/local/bin/", check=False)
    for v in VNFS:
        cmd(core, v, SCRIPT_VNF[v] if modo == "cadeia" else "vnf-transparente")


def pacotes(core, vnf: str, dev: str) -> int:
    out = cmd(core, vnf, f"tc -s filter show dev {dev} ingress pref 100")
    m = re.findall(r"Sent \d+ bytes (\d+) pkt", out)
    return max(map(int, m)) if m else 0


def ping(core, de: str, para: str) -> bool:
    rc, _ = core.node_command(SESSAO, N[de], f"ping -c 2 -W 2 -i 0.3 {para}", wait=True)
    return rc == 0


def testar(core) -> list:
    res = []
    time.sleep(2)
    antes = {v: (pacotes(core, v, "eth0"), pacotes(core, v, "eth1")) for v in VNFS}
    # todos os clientes chegam a todos os servidores (o bug de MACs afetava só alguns)
    for cli in CLIENTES:
        falhas = [srv for srv, (rede, host, _) in SERVIDORES.items()
                  if not ping(core, cli, f"{rede}.{host}")]
        res.append((f"ping {cli} → servidores e atacante", not falhas,
                    f"falhou: {', '.join(falhas)}" if falhas else ""))
    desc = {v: pacotes(core, v, "eth0") - antes[v][0] for v in VNFS}
    asc = {v: pacotes(core, v, "eth1") - antes[v][1] for v in VNFS}
    res.append(("descendente passa por fw→cl→po→sc", all(desc[v] > 0 for v in VNFS),
                " ".join(f"{v}:{desc[v]}" for v in VNFS)))
    res.append(("ascendente passa só por cl→fw", asc["fw"] > 0 and asc["cl"] > 0
                and asc["po"] == 0 and asc["sc"] == 0,
                " ".join(f"{v}:{asc[v]}" for v in VNFS)))
    # gargalo: iperf3 descendente srv-video → cli-video
    cmd(core, "srv-video", "iperf3 -s -D -1")
    time.sleep(0.5)
    rc, out = core.node_command(SESSAO, N["cli-video"],
                                "iperf3 -c 10.1.2.10 -R -t 5 -J", wait=True)
    try:
        import json
        mbps = json.loads(out)["end"]["sum_received"]["bits_per_second"] / 1e6
        res.append(("gargalo limita a ~10 Mbit/s (iperf3 descendente)",
                    7 < mbps <= GARGALO_KBIT / 1000 * 1.05, f"{mbps:.2f} Mbit/s"))
    except Exception:  # noqa: BLE001
        res.append(("gargalo limita a ~10 Mbit/s (iperf3 descendente)", False, out[-150:]))
    return res


def main() -> int:
    global IMAGEM_VNF
    ap = argparse.ArgumentParser()
    ap.add_argument("--parar", action="store_true", help="apaga a sessão")
    ap.add_argument("--sem-teste", action="store_true")
    ap.add_argument("--imagem", default="qosi/vnf:latest", help="imagem Docker das VNFs")
    ap.add_argument("--modo", choices=["transparente", "cadeia"],
                    help="só muda o modo das VNFs numa sessão já ligada")
    a = ap.parse_args()
    IMAGEM_VNF = a.imagem

    core = client.CoreGrpcClient()
    core.connect()
    if a.parar:
        ok = any(s.id == SESSAO for s in core.get_sessions()) and core.delete_session(SESSAO)
        print(f"sessão {SESSAO} apagada" if ok else f"sessão {SESSAO} não existia")
        return 0

    if a.modo:
        aplicar_modo(core, a.modo)
        print(f"VNFs em modo {a.modo}")
        return 0

    print("a criar a sessão no core-daemon...")
    construir(core)
    configurar(core)
    print(f"sessão {SESSAO} a correr  (bridge do nó de serviço: {bridge('ovs-sn')})")
    if a.sem_teste:
        return 0

    resultados = testar(core)
    print()
    for nome, ok, info in resultados:
        print(f"  [{'OK' if ok else 'FALHOU':^6}] {nome}" + (f"  ({info})" if info else ""))
    tudo_ok = all(ok for _, ok, _ in resultados)
    print("\nTOPOLOGIA:", "OK" if tudo_ok else "FALHOU")
    print(f"\nA sessão continua ligada. No GUI: Session → Sessions → {SESSAO} → Join.")
    print("Para desligar: make parar")
    return 0 if tudo_ok else 1


if __name__ == "__main__":
    sys.exit(main())
