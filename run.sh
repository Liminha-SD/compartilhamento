#!/usr/bin/env bash
# Ativa a venv e inicia o servidor de compartilhamento de tela
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

exec ./venv/bin/python screen_share.py "$@"
