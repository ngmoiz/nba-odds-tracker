#!/usr/bin/env bash
# Script de vérification pré-rapport : tests, lint, git status.
# À lancer avant tout rapport de fin de lot, sortie collée telle quelle.
#
# Passe par `uv run` : pytest et ruff résolvent ainsi le layout src/ même hors
# venv activé (sinon `pytest` nu échoue en ModuleNotFoundError sur analyzer/common).
# `set -e` : tout échec de pytest ou ruff fait sortir le script avec un code non
# nul et saute le message de succès final — un check.sh vert (exit 0) prouve donc
# réellement que les vérifications sont passées.

set -euo pipefail

echo "=========================================="
echo "NBA Odds Tracker - Vérification complète"
echo "=========================================="
echo ""

echo "1. Tests (uv run pytest -q)"
echo "--------------------"
uv run pytest -q
echo ""

echo "2. Lint (uv run ruff check)"
echo "--------------------"
uv run ruff check
echo ""

echo "3. Git status"
echo "-------------"
git status
echo ""

echo "=========================================="
echo "✅ Toutes les vérifications sont passées"
echo "=========================================="
