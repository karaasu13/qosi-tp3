#!/usr/bin/env python3
"""Configura o terminal do core-gui (duplo clique num nó abre um xterm nesse nó).

O sudo é preciso porque os nós do CORE pertencem ao root (core-daemon).
Corre com: /opt/core/venv/bin/python scripts/gui_config.py
"""
from core.gui import appconfig

TERMINAL = "xterm -fa Monospace -fs 11 -e sudo"

appconfig.check_directory()  # cria ~/.coregui/config.yaml se ainda não existir
cfg = appconfig.read()
if cfg.preferences.terminal != TERMINAL:
    cfg.preferences.terminal = TERMINAL
    appconfig.save(cfg)
    print(f"core-gui: terminal = {TERMINAL}")
