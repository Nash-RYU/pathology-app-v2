#!/usr/bin/env bash
# 病理画像AI診断支援の起動スクリプト
set -euo pipefail
cd "$(dirname "$0")"

# openslide (system lib) が conda 環境に入っている前提。
# 無い場合は: conda install -y -c conda-forge openslide
exec python pathology_app/main.py "$@"
