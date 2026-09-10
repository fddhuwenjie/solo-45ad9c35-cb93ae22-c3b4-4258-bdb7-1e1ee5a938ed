#!/usr/bin/env bash
# 一键启动：自动创建/复用项目内虚拟环境并运行 uvicorn
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv --without-pip .venv || true
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
  fi
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
