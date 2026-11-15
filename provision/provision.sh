#!/usr/bin/env bash
# Provisionamento da VM do TP3 (corre como root via Vagrant; pode voltar a correr-se).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

CORE_VERSAO="9.2.1"
CORE_DEB="core_${CORE_VERSAO}_amd64.deb"
CORE_URL="https://github.com/coreemu/core/releases/download/release-${CORE_VERSAO}/${CORE_DEB}"

echo ">>> desligar atualizações automáticas (prendem o apt no 1.º arranque)"
systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
pkill -f unattended-upgrade 2>/dev/null || true
while command -v fuser >/dev/null && fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do echo "à espera do lock do apt..."; sleep 5; done

echo ">>> pacotes base (5-15 min)"
apt-get update -q
apt-get install -y -q -o Dpkg::Progress-Fancy=0 \
  git make curl ca-certificates python3 python3-pip python3-venv \
  iproute2 ethtool nftables iputils-ping tcpdump tshark \
  openvswitch-switch docker.io \
  iperf3 irtt hping3 nmap python3-scapy \
  xauth x11-apps xterm \
  "linux-modules-extra-$(uname -r)" || \
apt-get install -y -qq git make curl python3 python3-pip python3-venv iproute2 ethtool \
  nftables iputils-ping tcpdump openvswitch-switch docker.io iperf3 irtt hping3 nmap \
  python3-scapy xauth

echo ">>> módulos do kernel usados pelas VNFs"
cat > /etc/modules-load.d/qosi.conf <<'EOF'
openvswitch
sch_htb
sch_fq_codel
sch_gred
sch_netem
sch_ingress
cls_flower
cls_u32
cls_matchall
act_mirred
act_pedit
act_csum
act_police
act_gact
EOF
while read -r m; do modprobe "$m" 2>/dev/null || echo "aviso: módulo $m indisponível"; done < /etc/modules-load.d/qosi.conf

echo ">>> CORE ${CORE_VERSAO}"
if ! dpkg -s core >/dev/null 2>&1; then
  cd /tmp
  curl -fsSL -o "${CORE_DEB}" "${CORE_URL}"
  echo "    (instala o ambiente Python do CORE, demora uns minutos)"
  apt-get install -y -q "./${CORE_DEB}"
fi
# switches do CORE (também no GUI) passam a ser bridges OVS
grep -q '^ovs *=' /opt/core/etc/core.conf || echo "ovs = 1" >> /opt/core/etc/core.conf
systemctl enable --now core-daemon

echo ">>> Docker + CORE: o Docker põe FORWARD a DROP, o que bloqueia o tráfego do CORE"
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/qosi.conf <<'EOF'
[Service]
ExecStartPost=/usr/sbin/iptables -P FORWARD ACCEPT
EOF
systemctl daemon-reload
systemctl enable --now openvswitch-switch docker
systemctl restart docker
usermod -aG docker vagrant

echo ">>> imagem base das VNFs"
if [ -f /vagrant/vnf/Dockerfile ]; then
  docker build -q -t qosi/vnf:latest /vagrant/vnf
fi

echo ">>> pronto. Testar com: vagrant ssh -c 'cd /vagrant && make teste'"
