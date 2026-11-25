#!/usr/bin/env bash
# Verifica se a VM tem tudo o que a proposta usa: ferramentas, serviços e funções do kernel.
# Uso: sudo scripts/verificar_ambiente.sh
set -u
ok=0; falhas=0
r() { if "$@" >/dev/null 2>&1; then printf "  [ OK ] %s\n" "$DESC"; ok=$((ok+1));
      else printf "  [FALHA] %s\n" "$DESC"; falhas=$((falhas+1)); fi; }

echo "Ferramentas"
for b in core-daemon core-gui docker ovs-vsctl ovs-ofctl tc nft ethtool iperf3 irtt \
         hping3 nmap tcpdump; do
  DESC="$b"; r command -v "$b"
done
DESC="core-python (/opt/core/venv)"; r test -x /opt/core/venv/bin/python
DESC="scapy"; r python3 -c "import scapy.all"

echo "Serviços"
for s in docker openvswitch-switch core-daemon; do DESC="$s ativo"; r systemctl is-active "$s"; done
DESC="imagem Docker qosi/vnf:latest"; r docker image inspect qosi/vnf:latest
DESC="iptables FORWARD = ACCEPT"; r sh -c "iptables -S FORWARD | head -1 | grep -q ACCEPT"

echo "Funções do kernel (testadas num par veth temporário)"
ip link add qv0 type veth peer name qv1 && ip link set qv0 up && ip link set qv1 up
tc qdisc add dev qv1 clsact 2>/dev/null
for q in "htb default 10" "fq_codel" "netem delay 1ms" "gred setup DPs 2 default 1" "tbf rate 1mbit burst 32k latency 50ms"; do
  DESC="qdisc ${q%% *}"; r sh -c "tc qdisc add dev qv0 root $q && tc qdisc del dev qv0 root"
done
for f in "protocol ip flower ip_proto udp action pass" \
         "protocol all matchall action pass" \
         "protocol all u32 match u32 0 0 action pass" \
         "protocol all u32 match u32 0 0 action mirred egress redirect dev qv0" \
         "protocol ip u32 match u32 0 0 action pedit ex munge ip dsfield set 0xb8 retain 0xfc pipe action csum ip" \
         "protocol ip u32 match u32 0 0 action police rate 1mbit burst 10k conform-exceed drop"; do
  set -- $f; nome="$3"; case "$f" in *mirred*) nome="mirred";; *pedit*) nome="pedit+csum";; *police*) nome="police";; esac
  DESC="tc $nome"; r sh -c "tc filter add dev qv1 ingress $f && tc filter del dev qv1 ingress"
done
DESC="nftables (inet, conntrack)"; r sh -c "nft add table inet qosi_t && nft add chain inet qosi_t c '{ type filter hook forward priority 0; }' && nft add rule inet qosi_t c ct state established accept && nft delete table inet qosi_t"
DESC="nftables bridge com conntrack (firewall)"; r sh -c "modprobe nf_conntrack_bridge; nft add table bridge qosi_b && nft add chain bridge qosi_b f '{ type filter hook forward priority 0; }' && nft add rule bridge qosi_b f ct state established accept && nft delete table bridge qosi_b"
DESC="OVS com datapath do kernel"; r sh -c "ovs-vsctl add-br qosi-t && ovs-vsctl del-br qosi-t"
ip link del qv0 2>/dev/null

echo
echo "Resultado: $ok OK, $falhas falha(s)"
[ "$falhas" -eq 0 ]
