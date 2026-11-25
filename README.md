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
make up                          # = vagrant up (~10 min na 1.ª vez)
make verificar teste
```

Com pouca RAM: `QOSI_MEM=3072 QOSI_CPUS=2 vagrant up`.

### No dia a dia

O `Makefile` funciona no host e na VM: no host cada comando é corrido dentro da VM por `vagrant ssh`, por isso não é preciso entrar nela.

```bash
make            # lista os comandos
make topologia  # cria a topologia completa e testa
make gui        # abre o core-gui no teu ecrã, já dentro da sessão
make parar
```

O `make gui` usa X11 por SSH: no host é preciso ter o `xauth` (Arch: `xorg-xauth`) e um servidor X (XWayland serve).

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

**Ver no GUI:** `make gui` (cria a sessão se ainda não existir e entra nela). Dá para abrir terminais nos nós (duplo clique) e correr `tcpdump`, `ping`, `iperf3`, etc.

**Desligar:** `make parar`.

> Notas sobre VNFs transparentes no CORE (o script já trata disto, em `topologias/ovs_util.py`):
> - As regras OpenFlow têm de estar instaladas **antes** de a VNF começar a reencaminhar; com a regra NORMAL por omissão, as duas interfaces da VNF no mesmo switch formam um ciclo L2.
> - O CORE liga cada interface ao switch através de uma bridge ptp intermédia que aprende MACs. Se um MAC for aprendido do lado errado (o que acontecia no arranque com o IPv6 dos clientes), essa bridge passa a deitar fora o tráfego para esse cliente. Por isso as bridges ptp passam a "fios" (porta A ⇄ porta B) e o IPv6 é desligado nas interfaces do host e dentro das VNFs.

## Tráfego e medição (baselines C0/C1)

`make cenario` (ou `make baseline` para C0 e C1 seguidos) lança as 4 classes ao mesmo tempo, todas no sentido descendente, e mede cada uma:

| Classe | Origem → destino | Ferramenta | Métricas |
|---|---|---|---|
| VoIP | srv-voip → cli-voip | `irtt`, 20 ms / 172 B | atraso num sentido p50/p95/p99, jitter, perdas |
| Vídeo | srv-video → cli-video | `iperf3` TCP a 4 Mbit/s | débito, retransmissões |
| Web | srv-web → cli-web | `curl` de 100 KB a cada 0,5 s | tempo de resposta p50/p95 |
| Bulk | srv-web → cli-bulk1–3 | `iperf3` TCP, 2 fluxos cada | débito total, equidade (Jain) |

Também regista a fila do gargalo a cada 0,2 s (ocupação em ms e descartes). Os nós partilham o relógio da VM, por isso o atraso num só sentido do `irtt` é exato.

| Config | Gargalo |
|---|---|
| C0 | HTB 10 Mbit/s + FIFO longa (~150 ms), sem QoS |
| C1 | HTB 10 Mbit/s + `fq_codel` |

```bash
make baseline                         # C0 e C1, 30 s cada
make baseline DURACAO=60 REP=5        # o que a proposta diz para os resultados finais
make cenario CONFIG=C1 DURACAO=60
```

Os resultados ficam em `resultados/<data>/`: `resumo.csv` (uma linha por execução), `fila.csv` e os JSON de cada ferramenta. No fim aparece uma tabela comparativa.

## VNFs (configuração C2)

`make cadeia` liga as funções nas 4 VNFs (`make transparente` volta a pô-las só a reencaminhar). Cada função é um script dentro da imagem (`vnf/bin/`), copiado para os contentores sempre que se muda de modo, por isso não é preciso recriar a topologia quando uma VNF é alterada.

| VNF | Script | O que faz | Valores iniciais |
|---|---|---|---|
| fw | `vnf-firewall` | bridge `br0` + nftables (família bridge, conntrack), política drop: só passam ligações começadas pelos clientes e as respostas | rede dos clientes 10.0.0.0/24 |
| cl | `vnf-classifier` | `tc flower` + `pedit`: DSCP pela origem (VoIP→EF, vídeo→AF41, web→BE, bulk→LE); apaga o DSCP marcado pelos hosts | — |
| po | `vnf-policer` | `tc police`: EF acima do débito é descartado; excesso de vídeo remarcado AF42; bulk limitado por cliente | EF 1 Mbit/s, vídeo 5 Mbit/s, bulk 3 Mbit/s/cliente |
| sc | `vnf-scheduler` | HTB a 9,5 Mbit/s na saída para o gargalo: EF prioritário, AF4 5 Mbit/s, BE 2 Mbit/s, LE 0,5 Mbit/s, todos com empréstimo | — |

Os valores podem mudar-se por variável de ambiente (p. ex. `EF_RATE=2mbit vnf-policer` dentro do contentor); é por aí que o controlador vai atuar.

> A firewall usa uma bridge em vez de `tc mirred` porque o `tc` corre antes do netfilter e o nftables nunca veria os pacotes. Separar AF41 de AF42 por precedência de descarte no scheduler exigiria GRED com `dsmark`, que saiu do kernel na versão 6.3; AF42 fica como marca (visível nos contadores e para o controlador).

**Testes E1** (`make e1`, ou `make e1 VNF=fw,cl` para só algumas): firewall bloqueia o atacante e deixa passar os clientes; cada classe chega com o DSCP certo e o DSCP dos hosts é apagado; o policer corta EF, remarca o vídeo em excesso e limita o bulk; com o gargalo cheio o VoIP e o vídeo ficam protegidos e a fila sai do gargalo.

**Cenário C2:** `make cenario CONFIG=C2`, ou os três de seguida com `make cenario CONFIG=C0,C1,C2`.

## Monitorização

`make monitor` arranca o exportador (`monitor/exportador.py`) em `http://<vm>:9101/metrics`, no formato do Prometheus: contadores da firewall, de cada regra do classifier e do policer, das classes do scheduler, da fila do gargalo e das regras OpenFlow do ovs-sn. O Prometheus e o Grafana vêm a seguir, a ler daqui.

## Estrutura

```
Vagrantfile               VM Ubuntu 24.04
provision/provision.sh    instala CORE, OVS, Docker, ferramentas e módulos do kernel
vnf/                      imagem das VNFs (qosi/vnf) e os scripts de cada função (vnf/bin)
topologias/               cadeia_minima.py (validação) e topologia.py (topologia completa)
testes/                   gerador de tráfego (trafego.py) e testes E1 das VNFs (e1.py)
monitor/                  exportador de métricas para o Prometheus
scripts/                  verificação do ambiente e utilitários
docs/                     proposta e notas
```

## Organização do repositório

- `main` tem sempre uma versão que passa `make teste`.
- Trabalho novo em ramos (`vnf-firewall`, `controlador`, …) e entra por pull request.
- Commits curtos em português, no imperativo: "Adiciona policer por classe".
