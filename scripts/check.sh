#!/usr/bin/env bash
# Script de vérification pré-rapport : tests, lint, git status
# À lancer avant tout rapport de fin de lot, sortie collée telle quelle

set -e  # Arrêt au premier échec

echo "=========================================="
echo "NBA Odds Tracker - Vérification complète"
echo "=========================================="
echo ""

echo "1. Tests (pytest -q)"
echo "--------------------"
pytest -q
echo ""

echo "2. Lint (ruff check)"
echo "--------------------"
ruff check
echo ""

echo "3. Git status"
echo "-------------"
git status
echo ""

echo "=========================================="
echo "✅ Toutes les vérifications sont passées"
echo "=========================================="
