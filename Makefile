# Correr dentro da VM, em /vagrant (vagrant ssh; cd /vagrant).
PY := sudo /opt/core/venv/bin/python

.PHONY: ajuda verificar imagem teste manter gui limpar

ajuda:
	@echo "make verificar  - confirma ferramentas, serviços e funções do kernel"
	@echo "make imagem     - (re)constrói a imagem Docker das VNFs"
	@echo "make teste      - cadeia mínima no CORE (srv - OVS - VNF - cli) e desliga"
	@echo "make manter     - igual, mas deixa a sessão ligada para inspecionar"
	@echo "make gui        - abre o core-gui (precisa de vagrant ssh -- -X)"
	@echo "make limpar     - remove sessões/contentores/bridges que tenham ficado"

verificar:
	sudo scripts/verificar_ambiente.sh

imagem:
	sudo docker build -q -t qosi/vnf:latest vnf

teste: imagem
	$(PY) topologias/cadeia_minima.py

manter: imagem
	$(PY) topologias/cadeia_minima.py --manter

gui:
	core-gui

limpar:
	-sudo /opt/core/venv/bin/core-cleanup
	-sudo docker rm -f vnf1
