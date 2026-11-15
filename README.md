# TP3 QoSI — Monitorização e Controlo de QoS via VNFs

Qualidade de Serviço na Internet, MEI, Universidade do Minho, 2026/27 — Grupo 10.

Cadeia de VNFs em contentores Docker, encadeadas por regras OpenFlow num Open vSwitch, numa rede emulada no CORE:

```
Firewall → Classifier → Policer → Scheduler → gargalo      (Monitor e controlador fora do caminho)
```

## Ambiente

Tudo corre numa VM Ubuntu 24.04 criada pelo Vagrant (VirtualBox), igual para os três.

| Componente | Versão |
|---|---|
| CORE | 9.2.1 (pacote .deb oficial, venv em `/opt/core/venv`) |
| Open vSwitch | o do Ubuntu 24.04 (3.3) |
| Docker | `docker.io` do Ubuntu |
| Ferramentas | iperf3, irtt, hping3, nmap, Scapy, tcpdump/tshark |

No `core.conf` fica `ovs = 1`, por isso os switches do CORE (incluindo os criados no GUI) são bridges OVS.

### Primeira vez

```bash
git clone git@github.com:karaasu13/qosi-tp3.git
cd qosi-tp3
vagrant up                       # ~10 min na 1.ª vez
vagrant ssh -c "cd /vagrant && make verificar && make teste"
```

Com pouca RAM: `QOSI_MEM=3072 QOSI_CPUS=2 vagrant up`.

### No dia a dia

```bash
vagrant ssh
cd /vagrant
make            # lista os comandos
```

O GUI do CORE abre no ecrã do host com `vagrant ssh -- -X` e depois `core-gui`.

## Cadeia mínima (validação de 16 nov)

`topologias/cadeia_minima.py` monta no CORE um servidor, um cliente, um switch OVS e uma VNF em Docker com duas interfaces. O OVS não aprende MACs: o tráfego só passa porque as regras por `in_port` o obrigam a atravessar a VNF.

```
srv 10.0.0.1 ── OVS ── cli 10.0.0.2
                 │ │
              eth0 eth1
               [ vnf1 ]   tc: eth0 ⇄ eth1 (mirred), remarca UDP:7000 → EF
```

`make teste` verifica:

1. ping cli → srv;
2. os contadores `tc` da VNF sobem nos dois sentidos (o tráfego passa mesmo por ela);
3. UDP para a porta 7000 chega ao cliente com DSCP EF (`tos 0xb8`), remarcado pela VNF;
4. iperf3 TCP;
5. sem as regras da VNF não há caminho; repostas, volta a haver.

Saída esperada:

```
  [  OK  ] ping cli → srv através da VNF
  [  OK  ] tráfego atravessa a VNF (contadores tc)
  [  OK  ] VNF remarca UDP:7000 para EF (DSCP 46)
  [  OK  ] iperf3 TCP cli → srv
  [  OK  ] sem regras para a VNF o tráfego não passa
  [  OK  ] regras repostas, ping volta a funcionar

CADEIA MÍNIMA: OK
```

A topologia fica guardada em `resultados/cadeia_minima.xml` e pode abrir-se no GUI.

## Topologia completa

`make topologia` cria no `core-daemon` a topologia da proposta e deixa-a a correr (sessão 10):

```
srv-voip  ─┐                                                       ┌─ cli-voip
srv-video ─┤                    nó de serviço (ovs-sn)             ├─ cli-video
srv-web   ─┼─ r1 ── r2 ── [ fw → cl → po → sc ] ══ sw-acesso ──────┼─ cli-web
atacante  ─┘  core   borda                    gargalo 10 Mbit/s    └─ cli-bulk1..3
```

| Rede | Endereços |
|---|---|
| Servidores (uma sub-rede cada, r1 = .1) | srv-voip 10.1.1.10, srv-video 10.1.2.10, srv-web 10.1.3.10, atacante 10.1.9.66 |
| r1 ↔ r2 | 10.255.0.1/30 ↔ 10.255.0.2/30 |
| Acesso (r2 = gateway 10.0.0.1) | cli-voip .11, cli-video .12, cli-web .13, cli-bulk1–3 .21–.23 |

- **Descendente** (servidores → clientes): r2 → fw → cl → po → sc → gargalo.
- **Ascendente**: gargalo → cl → fw → r2 (sem policiamento nem agendamento).
- As VNFs estão em **modo transparente** (`vnf-transparente`, dentro da imagem). Cada função entra por cima disto, com filtros `tc` de preferência < 100 ou regras `nft`.
- O gargalo é um FIFO (`tbf`) de 10 Mbit/s nos dois sentidos, com fila de ~150 ms (configuração C0).

No fim corre um auto-teste: pings dos clientes a todos os servidores, contadores das 4 VNFs (confirma o caminho em cada sentido) e iperf3 a passar no gargalo.

**Ver no GUI:** `vagrant ssh -- -X`, depois `core-gui` → *Session → Sessions* → sessão 10 → *Join*. Dá para abrir terminais nos nós (duplo clique) e correr `tcpdump`, `ping`, `iperf3`, etc.

**Desligar:** `make parar`.

> Notas sobre VNFs transparentes no CORE (o script já trata disto, em `topologias/ovs_util.py`):
> - As regras OpenFlow têm de estar instaladas **antes** de a VNF começar a reencaminhar; com a regra NORMAL por omissão, as duas interfaces da VNF no mesmo switch formam um ciclo L2.
> - O CORE liga cada interface ao switch através de uma bridge ptp intermédia que aprende MACs. Se um MAC for aprendido do lado errado (o que acontecia no arranque com o IPv6 dos clientes), essa bridge passa a deitar fora o tráfego para esse cliente. Por isso as bridges ptp passam a "fios" (porta A ⇄ porta B) e o IPv6 é desligado nas interfaces do host e dentro das VNFs.

## Estrutura

```
Vagrantfile               VM Ubuntu 24.04
provision/provision.sh    instala CORE, OVS, Docker, ferramentas e módulos do kernel
vnf/                      imagem base das VNFs (qosi/vnf) e vnf-transparente
topologias/               cadeia_minima.py (validação) e topologia.py (topologia completa)
scripts/                  verificação do ambiente e utilitários
docs/                     proposta e notas
```

## Organização do repositório

- `main` tem sempre uma versão que passa `make teste`.
- Trabalho novo em ramos (`vnf-firewall`, `controlador`, …) e entra por pull request.
- Commits curtos em português, no imperativo: "Adiciona policer por classe".
