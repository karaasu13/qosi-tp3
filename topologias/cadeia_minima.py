#!/usr/bin/env python3
"""
Cadeia mínima do TP3: valida a integração CORE + OVS + VNF em Docker.

    srv (10.0.0.1) ── OVS ── cli (10.0.0.2)
                       │ │
                    eth0 eth1
                     [ vnf1 ]   (Docker, transparente em L2, tc mirred)

O OVS não faz MAC learning: o tráfego só passa porque as regras OpenFlow
por in_port o obrigam a atravessar a VNF (srv → vnf1:eth0 → vnf1:eth1 → cli).
A VNF remarca o DSCP para EF (46) no tráfego UDP com destino à porta 7000,
para provar que o pacote é mesmo processado dentro dela.

Correr dentro da VM:
    sudo /opt/core/venv/bin/python topologias/cadeia_minima.py            # testa e desliga
    sudo /opt/core/venv/bin/python topologias/cadeia_minima.py --manter   # deixa a sessão ligada
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

from core.emulator.coreemu import CoreEmu
from core.emulator.data import InterfaceData
from core.emulator.enumerations import EventTypes
from core.nodes.base import CoreNode, Position
from core.nodes.docker import DockerNode
from core.nodes.network import SwitchNode

from ovs_util import desligar_ipv6_host, ptp_como_fios

RAIZ = Path(__file__).resolve().parent.parent
DSCP_EF_TOS = 0xB8  # DSCP 46 << 2
PORTA_EF = 7000


def sh(cmd: str) -> str:
    """Comando no host (fora dos nós)."""
    return subprocess.run(cmd, shell=True, check=True, text=True,
                          capture_output=True).stdout.strip()


def ofport(iface: str) -> int:
    return int(sh(f"ovs-vsctl get Interface {iface} ofport"))


def construir(session, imagem: str):
    ovs = session.add_node(SwitchNode, name="ovs", position=Position(x=400, y=200))

    opts = CoreNode.create_options()
    opts.services = []
    srv = session.add_node(CoreNode, name="srv", position=Position(x=150, y=200),
                           options=opts)
    cli = session.add_node(CoreNode, name="cli", position=Position(x=650, y=200),
                           options=opts)

    dopts = DockerNode.create_options()
    dopts.image = imagem
    dopts.model = ""  # modelo sem serviços por omissão (ex.: DefaultRoute) na VNF
    vnf = session.add_node(DockerNode, name="vnf1", position=Position(x=400, y=350),
                           options=dopts)

    # add_link devolve (iface do nó, iface do switch); a do switch é a porta no OVS
    _, srv_p = session.add_link(srv.id, ovs.id, InterfaceData(id=0, name="eth0", ip4="10.0.0.1", ip4_mask=24))
    _, cli_p = session.add_link(cli.id, ovs.id, InterfaceData(id=0, name="eth0", ip4="10.0.0.2", ip4_mask=24))
    _, v0_p = session.add_link(vnf.id, ovs.id, InterfaceData(id=0, name="eth0"))
    _, v1_p = session.add_link(vnf.id, ovs.id, InterfaceData(id=1, name="eth1"))
    return ovs, srv, cli, vnf, {"srv": srv_p, "cli": cli_p, "v0": v0_p, "v1": v1_p}


def configurar_vnf(vnf) -> bool:
    """VNF transparente: o que entra em eth0 sai em eth1 e vice-versa.

    Devolve False se o kernel não suportar a remarcação DSCP (flower/pedit).
    """
    for i in ("eth0", "eth1"):
        vnf.cmd(f"ethtool -K {i} gro off gso off tso off")
        vnf.cmd(f"ip link set {i} promisc on")
        vnf.cmd(f"tc qdisc replace dev {i} clsact")
    # reencaminhamento eth0 <-> eth1 (u32 "match u32 0 0" apanha tudo, incluindo ARP)
    vnf.cmd("tc filter add dev eth0 ingress pref 100 protocol all u32 match u32 0 0 "
            "action mirred egress redirect dev eth1")
    vnf.cmd("tc filter add dev eth1 ingress pref 100 protocol all u32 match u32 0 0 "
            "action mirred egress redirect dev eth0")
    # remarcação DSCP no sentido descendente: tc flower + pedit
    try:
        vnf.cmd("tc filter add dev eth0 ingress pref 10 protocol ip flower ip_proto udp "
                f"dst_port {PORTA_EF} action pedit ex munge ip dsfield set "
                f"{DSCP_EF_TOS:#x} retain 0xfc pipe action csum ip pipe "
                "action mirred egress redirect dev eth1")
        return True
    except Exception:  # noqa: BLE001
        return False  # kernel sem cls_flower/act_pedit


def regras_cadeia(br: str, p: dict) -> None:
    sh(f"ovs-vsctl set-fail-mode {br} secure")
    sh(f"ovs-ofctl del-flows {br}")
    flows = [
        "priority=0,actions=drop",
        f"priority=100,in_port={p['srv']},actions=output:{p['v0']}",
        f"priority=100,in_port={p['v1']},actions=output:{p['cli']}",
        f"priority=100,in_port={p['cli']},actions=output:{p['v1']}",
        f"priority=100,in_port={p['v0']},actions=output:{p['srv']}",
    ]
    for f in flows:
        sh(f"ovs-ofctl add-flow {br} '{f}'")


def pacotes_tc(vnf, dev: str, pref: int) -> int:
    out = vnf.cmd(f"tc -s filter show dev {dev} ingress pref {pref}")
    m = re.findall(r"Sent \d+ bytes (\d+) pkt", out)
    return max(map(int, m)) if m else 0


def ping(no, destino: str) -> bool:
    try:
        no.cmd(f"ping -c 3 -W 2 -i 0.3 {destino}")
        return True
    except Exception:
        return False


def testar(ovs, srv, cli, vnf, p: dict, tem_dscp: bool) -> list:
    res = []
    br = ovs.brname

    antes = pacotes_tc(vnf, "eth0", 100), pacotes_tc(vnf, "eth1", 100)
    ok = ping(cli, "10.0.0.1")
    res.append(("ping cli → srv através da VNF", ok, ""))

    n_desc = pacotes_tc(vnf, "eth0", 100) - antes[0]
    n_asc = pacotes_tc(vnf, "eth1", 100) - antes[1]
    res.append(("tráfego atravessa a VNF (contadores tc)", n_desc > 0 and n_asc > 0,
                f"durante o ping: eth0→eth1 {n_desc} pkts, eth1→eth0 {n_asc} pkts; "
                f"antes do ping já havia {antes[0]}/{antes[1]}"))

    # DSCP: srv envia UDP com TOS 0, cli deve receber com TOS 0xb8 (EF)
    if tem_dscp:
        cap = "/tmp/qosi_dscp.txt"
        cli.cmd(f"timeout 6 tcpdump -l -c 3 -nn -v -i eth0 udp dst port {PORTA_EF} "
                f"> {cap} 2>&1", wait=False, shell=True)
        time.sleep(1.5)
        srv.cmd("python3 -c \"import socket,time;s=socket.socket(2,2);"
                f"[(s.sendto(b'x'*100,('10.0.0.2',{PORTA_EF})),time.sleep(0.2)) "
                "for _ in range(5)]\"", shell=True)
        time.sleep(1.5)
        txt = Path(cap).read_text() if Path(cap).exists() else ""
        ok = "tos 0xb8" in txt
        res.append(("VNF remarca UDP:7000 para EF (DSCP 46)", ok,
                    "tos 0xb8 visto no cli" if ok else txt[-200:]))
    else:
        res.append(("VNF remarca UDP:7000 para EF (DSCP 46)", None,
                    "SALTADO: o kernel não tem cls_flower/act_pedit"))

    # débito TCP com iperf3
    srv.cmd("iperf3 -s -D -1")
    time.sleep(0.5)
    try:
        j = json.loads(cli.cmd("iperf3 -c 10.0.0.1 -t 4 -J"))
        mbps = j["end"]["sum_received"]["bits_per_second"] / 1e6
        res.append(("iperf3 TCP cli → srv", mbps > 1, f"{mbps:.1f} Mbit/s"))
    except Exception as e:  # noqa: BLE001
        res.append(("iperf3 TCP cli → srv", False, str(e)[:120]))

    # sem as regras da VNF não há caminho: prova que o steering é obrigatório
    for port in (p["srv"], p["cli"]):
        sh(f"ovs-ofctl del-flows {br} in_port={port}")
    sem = ping(cli, "10.0.0.1")
    res.append(("sem regras para a VNF o tráfego não passa", not sem, ""))
    regras_cadeia(br, p)
    res.append(("regras repostas, ping volta a funcionar", ping(cli, "10.0.0.1"), ""))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imagem", default="qosi/vnf:latest")
    ap.add_argument("--manter", action="store_true",
                    help="não desliga a sessão no fim (para ver no core-gui)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.WARNING)

    coreemu = CoreEmu()
    session = coreemu.create_session(_id=90)  # id alto: não colide com sessões do GUI
    session.options.set("ovs", "1")
    session.set_state(EventTypes.CONFIGURATION_STATE)
    try:
        ovs, srv, cli, vnf, ifs = construir(session, a.imagem)
        session.instantiate()
        p = {k: ofport(i.name) for k, i in ifs.items()}
        print(f"bridge {ovs.brname}  portas OpenFlow: {p}")
        desligar_ipv6_host(session.id)
        ptp_como_fios(session.id)
        # regras OpenFlow ANTES de ligar o reencaminhamento na VNF: com a regra
        # NORMAL por omissão do OVS, eth0 e eth1 da VNF no mesmo switch formam um
        # ciclo L2 e qualquer broadcast (ARP, IPv6 ND) gera uma tempestade.
        regras_cadeia(ovs.brname, p)
        tem_dscp = configurar_vnf(vnf)
        time.sleep(1)

        resultados = testar(ovs, srv, cli, vnf, p, tem_dscp)
        print()
        for nome, ok, info in resultados:
            marca = {True: "OK", False: "FALHOU", None: "--"}[ok]
            print(f"  [{marca:^6}] {nome}" + (f"  ({info})" if info else ""))
        tudo_ok = all(ok is not False for _, ok, _ in resultados)
        print("\nCADEIA MÍNIMA:", "OK" if tudo_ok else "FALHOU")

        out = RAIZ / "resultados"
        out.mkdir(exist_ok=True)
        session.save_xml(out / "cadeia_minima.xml")

        if a.manter:
            print(f"\nSessão {session.id} ligada. Fluxos: sudo ovs-ofctl dump-flows "
                  f"{ovs.brname}\nVNF: sudo docker exec -it vnf1 bash")
            input("Enter para desligar...")
        return 0 if tudo_ok else 1
    finally:
        coreemu.shutdown()


if __name__ == "__main__":
    sys.exit(main())
