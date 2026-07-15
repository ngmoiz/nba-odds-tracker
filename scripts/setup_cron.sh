#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# setup_cron.sh — Installe les jobs cron WSL2 pour le NBA Odds Tracker.
#
# Planning (heure locale Europe/Paris, calé sur les tip-offs WNBA actuels) :
#
# Les matchs WNBA se jouent en soirée heure US (19:00–22:00 ET = 01:00–04:00
# du matin à Paris). Les fenêtres H-6/H-3/H-1 sont donc décalées vers la nuit.
#
#   Collecte du matin    : 09:00  → découverte + cotes d'ouverture
#   Collecte après-midi  : 15:00  → relevé intermédiaire
#   Collecte H-6         : 20:00  → tip-offs ~02:00 Paris (H-6)
#   Collecte H-3         : 23:00  → tip-offs ~02:00 Paris (H-3)
#   Collecte H-1         : 01:00  → fenêtre de décision H-1 (tip-offs 01:00–02:30)
#   Évaluateur           : 09:30  → bilan du matin (après la collecte)
#
# Budget : 5 collectes/jour × 3 crédits = 15 crédits/jour ≈ 450/mois.
#
# ⚠️ Limite structurelle cron-WSL2 : si le PC est éteint ou en veille, les jobs
# ne s'exécutent pas. Pour la validation 7 jours, laisser le PC allumé en
# permanence. Cette limite motive le déploiement EC2 (phase 3) où cron tourne
# sur un serveur 24/7.
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
MARKER_START="# nba-odds-tracker (1.7) — début"
MARKER_END="# nba-odds-tracker (1.7) — fin"

echo "Installation des jobs cron pour NBA Odds Tracker..."
echo "Répertoire projet : $PROJECT_DIR"

# Supprime d'anciens jobs marqués (idempotent) : du marqueur de début au marqueur de fin.
if crontab -l 2>/dev/null | grep -q "$MARKER_START"; then
    crontab -l 2>/dev/null | sed "/$MARKER_START/,/$MARKER_END/d" > /tmp/nba_cron_new
else
    crontab -l 2>/dev/null > /tmp/nba_cron_new || true
fi

# Ajoute les nouveaux jobs.
# `docker compose run --rm` : exécute le one-shot puis supprime le conteneur.
# `--no-deps` : ne démarre pas le listener (déjà en route via `docker compose up -d`).
cat >> /tmp/nba_cron_new <<EOF
$MARKER_START
# Collecte du matin (découverte + cotes d'ouverture)
0 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte de l'après-midi (relevé intermédiaire)
0 15 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-6 (tip-offs WNBA ~02:00 Paris)
0 20 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-3 (tip-offs WNBA ~02:00 Paris)
0 23 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Collecte H-1 (fenêtre de décision, tip-offs 01:00–02:30 Paris)
0 1 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector 2>&1 | logger -t nba-collector
# Évaluateur (bilan du matin + rapport hebdo le lundi)
30 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps evaluator 2>&1 | logger -t nba-evaluator
$MARKER_END
EOF

crontab /tmp/nba_cron_new
rm /tmp/nba_cron_new

echo ""
echo "Jobs cron installés :"
crontab -l | sed -n "/$MARKER_START/,/$MARKER_END/p"
echo ""
echo "Bot d'écoute : lance-le en continu avec :"
echo "  cd $PROJECT_DIR && docker compose up -d listener"
echo ""
echo "⚠️  Limite cron-WSL2 : le PC doit rester allumé (pas de veille)."
echo "    Cette limite motive le déploiement EC2 (phase 3)."
echo ""
echo "Pour désinstaller les jobs : crontab -l | grep -v 'nba-odds-tracker' | crontab -"