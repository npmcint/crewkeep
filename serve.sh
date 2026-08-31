#!/bin/bash
# CrewKeep dev server — sources ~/jobhunt/.auth.env for LLM keys (dev-only).
cd "$(dirname "$0")"
if [ -f ~/jobhunt/.auth.env ]; then
  set -a; . ~/jobhunt/.auth.env; set +a
fi
export CREWKEEP_HOST="${CREWKEEP_HOST:-0.0.0.0}"
export CREWKEEP_PORT="${CREWKEEP_PORT:-8091}"
exec ./.venv/bin/python crewkeep.py serve
