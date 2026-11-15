# Imagem base de todas as VNFs (Firewall, Classifier, Policer, Scheduler).
# O CORE arranca-a com --privileged e --net=none e liga as interfaces depois.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      iproute2 ethtool nftables iputils-ping tcpdump procps kmod \
      python3 python3-pip iperf3 \
    && rm -rf /var/lib/apt/lists/*

COPY bin/ /usr/local/bin/

# agente Python de cada VNF (a acrescentar): /opt/vnf/agente.py
# Sem WORKDIR: o CORE escreve ficheiros com caminhos relativos à raiz do contentor.
CMD ["tail", "-f", "/dev/null"]
