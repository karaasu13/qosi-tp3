"""
Ajustes ao que o CORE cria no host, comuns às topologias.

O CORE liga cada interface de um nó a um switch através de uma bridge ptp
intermédia (p.<id>.<sessão>) com a regra NORMAL, ou seja, com aprendizagem de MACs.
Com VNFs transparentes isso é perigoso: se um MAC for aprendido do lado errado
(por exemplo durante o arranque, antes das regras da cadeia), a bridge passa a
deitar fora o tráfego para esse destino. Aqui transformamos essas bridges em
fios e desligamos o IPv6 nas interfaces do host, que só geram ruído.
"""

import re
import subprocess
from pathlib import Path


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr.strip()}")
    return r.stdout.strip()


def short_id(sid: int) -> str:
    return f"{(sid >> 8) ^ (sid & 0xFF):x}"


def desligar_ipv6_host(sid: int) -> int:
    """Desliga o IPv6 nas interfaces do host criadas pelo CORE para esta sessão."""
    suf = f".{short_id(sid)}"
    n = 0
    for d in Path("/sys/class/net").iterdir():
        if re.match(r"^(veth|beth|b\.|p\.)", d.name) and d.name.endswith(suf):
            f = Path(f"/proc/sys/net/ipv6/conf/{d.name}/disable_ipv6")
            if f.exists():
                f.write_text("1")
                n += 1
    return n


def ptp_como_fios(sid: int) -> int:
    """Bridges ptp da sessão passam a fios: porta A <-> porta B, sem aprender MACs."""
    suf = f".{short_id(sid)}"
    n = 0
    for br in sh("ovs-vsctl list-br").split():
        if not (br.startswith("p.") and br.endswith(suf)):
            continue
        portas = sh(f"ovs-vsctl list-ports {br}").split()
        if len(portas) != 2:
            continue
        a, b = (int(sh(f"ovs-vsctl get Interface {p} ofport")) for p in portas)
        sh(f"ovs-vsctl set-fail-mode {br} secure")
        sh(f"ovs-ofctl del-flows {br}")
        sh(f"ovs-ofctl add-flow {br} 'priority=100,in_port={a},actions=output:{b}'")
        sh(f"ovs-ofctl add-flow {br} 'priority=100,in_port={b},actions=output:{a}'")
        n += 1
    return n


def limpar_macs(br: str) -> None:
    sh(f"ovs-appctl fdb/flush {br}", check=False)
