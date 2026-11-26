# Funciona dentro e fora da VM: no host, cada alvo é reencaminhado para a VM
# (vagrant ssh), por isso basta "make topologia" ou "make gui" na pasta do repo.
PY := sudo /opt/core/venv/bin/python
SESSAO := 10
TARGETS := ajuda verificar imagem teste manter topologia parar gui cenario baseline cadeia transparente e1 monitor limpar
CONFIG ?= C0
DURACAO ?= 30
REP ?= 1

ifneq ($(shell hostname),qosi-tp3)
# ---------------------------------------------------------------- no host
.PHONY: $(TARGETS) up
.DEFAULT_GOAL := ajuda

up:
	vagrant up

$(TARGETS):
	@vagrant ssh -c "cd /vagrant && make $@ $(MAKEOVERRIDES)"

else
# ---------------------------------------------------------------- na VM
.PHONY: $(TARGETS)

ajuda:
	@echo "make verificar  - confirma ferramentas, serviços e funções do kernel"
	@echo "make imagem     - (re)constrói a imagem Docker das VNFs"
	@echo "make teste      - cadeia mínima no CORE (srv - OVS - VNF - cli) e desliga"
	@echo "make manter     - igual, mas deixa a sessão ligada para inspecionar"
	@echo "make topologia  - topologia completa no core-daemon (fica ligada)"
	@echo "make gui        - abre o core-gui já na sessão $(SESSAO) (cria-a se não existir)"
	@echo "make parar      - apaga a sessão da topologia"
	@echo "make cenario    - tráfego das 4 classes e medição (CONFIG=C0|C1 DURACAO=30 REP=1)"
	@echo "make baseline   - cenário em C0 e C1 seguidos, com tabela comparativa"
	@echo "make cadeia     - VNFs com as funções ligadas (firewall, classifier, policer, scheduler)"
	@echo "make transparente - VNFs só a reencaminhar (como em C0/C1)"
	@echo "make e1         - teste funcional de cada VNF (VNF=fw,cl,po,sc)"
	@echo "make monitor    - exportador de métricas em http://<vm>:9101/metrics"
	@echo "make limpar     - remove sessões/contentores/bridges que tenham ficado"
	@echo "(no host: 'make up' cria/arranca a VM)"

verificar:
	sudo scripts/verificar_ambiente.sh

imagem:
	sudo docker build -q -t qosi/vnf:latest vnf

teste: imagem
	$(PY) topologias/cadeia_minima.py

manter: imagem
	$(PY) topologias/cadeia_minima.py --manter

topologia: imagem
	$(PY) topologias/topologia.py

parar:
	$(PY) topologias/topologia.py --parar

gui:
	@if [ -z "$$DISPLAY" ]; then echo "Sem DISPLAY: corre 'make gui' no host (ou entra com 'vagrant ssh -- -X')."; exit 1; fi
	@$(TOPOLOGIA_LIGADA) || $(MAKE) --no-print-directory topologia
	@command -v xterm >/dev/null || sudo apt-get install -y -q xterm
	@/opt/core/venv/bin/python scripts/gui_config.py
	core-gui -s $(SESSAO)

TOPOLOGIA_LIGADA = /opt/core/venv/bin/python -c "from core.api.grpc import client; c=client.CoreGrpcClient(); c.connect(); exit(0 if any(s.id==$(SESSAO) for s in c.get_sessions()) else 1)"

cenario:
	@$(TOPOLOGIA_LIGADA) || $(MAKE) --no-print-directory topologia
	$(PY) testes/trafego.py --config $(CONFIG) --duracao $(DURACAO) --repeticoes $(REP)

baseline:
	@$(TOPOLOGIA_LIGADA) || $(MAKE) --no-print-directory topologia
	$(PY) testes/trafego.py --config C0,C1 --duracao $(DURACAO) --repeticoes $(REP)

VNF ?= fw,cl,po,sc

cadeia:
	@$(TOPOLOGIA_LIGADA) || $(MAKE) --no-print-directory topologia
	$(PY) topologias/topologia.py --modo cadeia

transparente:
	$(PY) topologias/topologia.py --modo transparente

e1: imagem
	@$(TOPOLOGIA_LIGADA) || $(MAKE) --no-print-directory topologia
	$(PY) testes/e1.py --vnf $(VNF)

monitor:
	$(PY) monitor/exportador.py

limpar:
	-$(PY) topologias/topologia.py --parar
	-sudo /opt/core/venv/bin/core-cleanup
	-sudo docker rm -f vnf1 fw cl po sc
	-for b in $$(sudo ovs-vsctl list-br | grep -E '^[bp]\.[0-9]+\.(a|5a)$$'); do sudo ovs-vsctl del-br $$b; done

endif
