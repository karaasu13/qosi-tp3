#!/bin/sh
# Funções comuns às VNFs (incluído com ". vnf-comum.sh").
# Convenção: eth0 = lado dos servidores (Internet), eth1 = lado dos clientes.
# Sentido descendente: entra em eth0, sai em eth1.

# DSCP no campo TOS (DSCP << 2)
TOS_EF=0xb8     # 46  VoIP
TOS_AF41=0x88   # 34  vídeo
TOS_AF42=0x90   # 36  vídeo em excesso (maior precedência de descarte)
TOS_BE=0x00     # 0   web e o resto
TOS_LE=0x04     # 1   bulk (Lower Effort, RFC 8622)

limpar() {
  for i in eth0 eth1; do
    tc qdisc del dev "$i" clsact 2>/dev/null || true
    tc qdisc del dev "$i" root 2>/dev/null || true
  done
  ip link del br0 2>/dev/null || true
  nft flush ruleset 2>/dev/null || true
}

base() {
  # a VNF é transparente: não origina tráfego próprio (IPv6 RS/MLD/DAD)
  sysctl -qw net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
  sysctl -qw net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
  for i in eth0 eth1; do
    ethtool -K "$i" gro off gso off tso off 2>/dev/null || true
    ip link set "$i" up promisc on
  done
}

# reencaminhamento eth0 <-> eth1 com tc (pref 100; as funções usam pref < 100)
encaminhar_tc() {
  for i in eth0 eth1; do tc qdisc replace dev "$i" clsact; done
  tc filter add dev eth0 ingress pref 100 protocol all u32 match u32 0 0 \
    action mirred egress redirect dev eth1
  tc filter add dev eth1 ingress pref 100 protocol all u32 match u32 0 0 \
    action mirred egress redirect dev eth0
}

# remarca o DSCP e reencaminha: remarcar DEV PREF TOS SAIDA <match flower...>
remarcar() {
  dev=$1 pref=$2 tos=$3 saida=$4; shift 4
  tc filter add dev "$dev" ingress pref "$pref" protocol ip flower "$@" \
    action pedit ex munge ip dsfield set "$tos" retain 0xfc pipe \
    action csum ip pipe \
    action mirred egress redirect dev "$saida"
}
