"""
Leitura dos contadores das VNFs e do gargalo.

Serve os testes E1, o cenário de tráfego e o exportador para o Prometheus.
Cada função recebe `ex(vnf, comando) -> str`, que corre um comando dentro da
VNF (pelo gRPC do CORE ou por docker exec).
"""

import json
import re

# o que cada regra (pref) faz, para dar nomes legíveis aos contadores
REGRAS = {
    "cl": {("eth0", 10): "desc_ef", ("eth0", 11): "desc_af41", ("eth0", 12): "desc_web",
           ("eth0", 13): "desc_le", ("eth0", 90): "desc_be_resto",
           ("eth1", 10): "asc_ef", ("eth1", 90): "asc_be_resto"},
    "po": {("eth0", 10): "ef", ("eth0", 20): "af41", ("eth0", 21): "af41_excesso_af42",
           ("eth0", 30): "le_cli_bulk1", ("eth0", 31): "le_cli_bulk2", ("eth0", 32): "le_cli_bulk3"},
}
CLASSES_SC = {"1:10": "ef", "1:20": "af4", "1:30": "be", "1:40": "le"}


def firewall(ex) -> dict:
    """{contador: pacotes} da tabela nftables da firewall."""
    out = ex("fw", "nft -j list counters table bridge fw")
    res = {}
    for o in json.loads(out or "{}").get("nftables", []):
        c = o.get("counter")
        if c:
            res[c["name"]] = c["packets"]
    return res


def filtros(ex, vnf: str, dev: str) -> dict:
    """{pref: {"pacotes", "descartes"}} dos filtros tc de ingress de `dev`.

    pacotes = pacotes que chegaram à regra; descartes = os que a regra deitou fora
    (no policer, os que excederam o débito)."""
    out = ex(vnf, f"tc -s -j filter show dev {dev} ingress")
    res = {}
    for f in json.loads(out or "[]"):
        acoes = f.get("options", {}).get("actions", [])
        if not acoes:
            continue
        st = [a.get("stats", {}) for a in acoes]
        r = res.setdefault(f["pref"], {"pacotes": 0, "descartes": 0})
        r["pacotes"] += st[0].get("packets", 0)  # a 1.ª ação vê todos os pacotes da regra
        r["descartes"] += sum(s.get("drops", 0) for s in st)
    return res


def regras(ex, vnf: str) -> dict:
    """Contadores com nome para o classifier e o policer: {nome: {...}}."""
    nomes = REGRAS[vnf]
    res = {}
    for dev in ("eth0", "eth1"):
        for pref, v in filtros(ex, vnf, dev).items():
            if (dev, pref) in nomes:
                res[nomes[(dev, pref)]] = v
    return res


_CLASSE = re.compile(
    r"class htb (\S+) .*?\n\s*Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+)"
    r".*?\n\s*backlog (\d+)b (\d+)p", re.S)


def classes_htb(texto: str) -> dict:
    """Interpreta `tc -s class show` (o iproute2 6.1 não tem JSON para classes)."""
    res = {}
    for h, b, p, d, o, bb, bp in _CLASSE.findall(texto):
        res[h] = {"bytes": int(b), "pacotes": int(p), "descartes": int(d),
                  "backlog_bytes": int(bb), "backlog_pkts": int(bp)}
    return res


def scheduler(ex) -> dict:
    """{classe: {...}} das classes HTB do scheduler (ef, af4, be, le)."""
    cls = classes_htb(ex("sc", "tc -s class show dev eth1"))
    return {CLASSES_SC[h]: v for h, v in cls.items() if h in CLASSES_SC}


def tudo(ex) -> dict:
    """Todos os contadores da cadeia (só faz sentido em modo cadeia)."""
    return {"fw": firewall(ex), "cl": regras(ex, "cl"), "po": regras(ex, "po"),
            "sc": scheduler(ex)}


def diferenca(depois: dict, antes: dict):
    """depois - antes, recursivamente (para contar só o que aconteceu num teste)."""
    if isinstance(depois, dict):
        return {k: diferenca(v, antes.get(k, 0 if not isinstance(v, dict) else {}))
                for k, v in depois.items()}
    return depois - (antes or 0)
