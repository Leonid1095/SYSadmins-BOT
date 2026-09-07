#!/bin/bash
# Один проход сторожа: собрать факты → найти дельту → разобрать → уведомить.
#
# Конвейер выстроен так, что дорогая часть включается последней: без дельты
# analyst.py возвращает "analysed: false", и ни модель, ни Telegram не трогаются.
#
# Работает под plg: credentials подписки принадлежат ему, а телеметрию, которой
# нужен root, сборщик берёт узкими правилами sudoers.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
PY="./venv/bin/python"

"$PY" watchdog/collect.py \
  | "$PY" watchdog/delta.py \
  | "$PY" watchdog/analyst.py \
  | "$PY" watchdog/notify.py
