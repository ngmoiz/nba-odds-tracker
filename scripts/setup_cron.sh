#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# setup_cron.sh — Installe les jobs cron WSL2 pour le NBA Odds Tracker.
#
# Planning (heure locale Europe/Paris, calé sur les tip-offs NBA nocturnes) :
#
#   Collecte du matin    : 09:00  → découverte + cotes d'ouverture
#   Collecte après-midi  : 15:00  → relevé intermédiaire
#   Collecte H-6         : 18:00  → premier bloc de matchs ~00:00 UTC (H-6)
#   Collecte H-3         : 21:00  →
#   Collecte H-1         : 23:00  → fenêtre de décision H-1
#   Évaluateur           : 09:30  → bilan du matin (après la collecte)
#
# Le bot d'écoute (listener) tourne en continu via docker-compose (pas de cron).
#
# Usage :
#   chmod +x scripts/setup_cron.sh
#   ./scripts/setup_cron.sh
#
# Pour désinstaller : crontab -l | grep -v 'nba-odds-tracker' | crontab -
# ─────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRON_MARKER="# nba-odds-tracker (1.7)"

echo "Installation des jobs cron pour NBA Odds Tracker..."
echo "Répertoire projet : $PROJECT_DIR"

# Supprime d'anciens jobs marqués (idempotent).
crontab -l 2>/dev/null | sed "/$CRON_MARKER/,+5d" | { cat; echo; } > /tmp/nba_cron_new

# Ajoute les nouveaux jobs.
# `docker compose run --rm` : exécute le one-shot puis supprime le conteneur.
# `--no-deps` : ne démarre pas le listener (déjà en route via `docker compose up -d`).
cat >> /tmp/nba_cron_new <<EOF
$CRON_MARKER
# Collecte du matin (découverte + cotes d'ouverture)
0 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte de l'après-midi (relevé intermédiaire)
0 15 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-6
0 18 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-3
0 21 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-1 (fenêtre de décision)
0 23 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Évaluateur (bilan du matin + rapport hebdo le lundi)
30 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps evaluator 2>&1 | logger -t nba-evaluator
# Fin nba-odds-tracker
EOF

crontab /tmp/nba_cron_new
rm /tmp/nba_cron_new

echo ""
echo "Jobs cron installés :"
crontab -l | grep -A 20 "$CRON_MARKER"
echo ""
echo "Bot d'écoute : lance-le en continu avec :"
echo "  cd $PROJECT_DIR && docker compose up -d listener"
echo ""
echo "Pour désinstaller les jobs : crontab -l | grep -v 'nba-odds-tracker' | crontab -"