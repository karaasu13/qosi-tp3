# Funciona dentro e fora da VM: no host, cada alvo é reencaminhado para a VM
# (vagrant ssh), por isso basta "make topologia" ou "make gui" na pasta do repo.
PY := sudo /opt/core/venv/bin/python
SESSAO := 10
TARGETS := ajuda verificar imagem teste manter topologia parar gui limpar

ifneq ($(shell hostname),qosi-tp3)
# ---------------------------------------------------------------- no host
.PHONY: $(TARGETS) up
.DEFAULT_GOAL := ajuda

up:
	vagrant up

$(TARGETS):
	@vagrant ssh -c "cd /vagrant && make $@"

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
	@/opt/core/venv/bin/python -c "from core.api.grpc import client; c=client.CoreGrpcClient(); c.connect(); exit(0 if any(s.id==$(SESSAO) for s in c.get_sessions()) else 1)" \
		|| $(MAKE) --no-print-directory topologia
	@command -v xterm >/dev/null || sudo apt-get install -y -q xterm
	@/opt/core/venv/bin/python scripts/gui_config.py
	core-gui -s $(SESSAO)

limpar:
	-sudo /opt/core/venv/bin/core-cleanup
	-sudo docker rm -f vnf1 fw cl po sc

endif
