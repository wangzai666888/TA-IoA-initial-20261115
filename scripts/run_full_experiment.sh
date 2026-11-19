#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m co_ioa.experiments --backend "${BACKEND:-ioa}" --planner "${PLANNER:-rule}" --repetitions "${REPETITIONS:-1}" --seed "${SEED:-0}" --output "${OUTPUT:-runs/full_experiment}"
