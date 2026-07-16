#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# setup_cron.sh — Installe les jobs cron WSL2 pour le NBA Odds Tracker.
#
# Planning (heure locale Europe/Paris, calé sur les tip-offs WNBA actuels) :
#
# Les matchs WNBA se jouent en soirée heure US (19:00–22:00 ET = 01:00–04:00
# du matin à Paris). Les fenêtres H-6/H-3/H-1 sont donc décalées vers la nuit.
#
#   Collecte du matin    : 09:00  → découverte + cotes d'ouverture (INCONDITIONNELLE)
#   Collecte après-midi  : 15:00  → relevé intermédiaire (conditionnelle)
#   Collecte H-6         : 20:00  → tip-offs ~02:00 Paris (conditionnelle)
#   Collecte H-3         : 23:00  → tip-offs ~02:00 Paris (conditionnelle)
#   Collecte H-1 (bloc1) : 01:00  → fenêtre de décision (tip-offs 01:00–03:00 Paris)
#   Collecte H-1 (bloc2) : 02:45  → fenêtre de décision (tip-offs 03:00–04:45 Paris, côte Ouest)
#   Évaluateur           : 09:30  → bilan du matin (après la collecte)
#
# Collectes conditionnelles : les créneaux 15:00–02:45 ne consomment aucun crédit
# si aucun match actif n'est en base (skip en amont de l'appel API). Le créneau
# 09:00 (--morning) est inconditionnel : il découvre les nouveaux matchs.
#
# Garde de réserve : si le quota restant passe sous quota.reserve (config.yaml),
# les collectes non essentielles sont sautées + notification Telegram (dédupliquée).
# La collecte du matin rafraîchit le quota et lève la garde au reset mensuel.
#
# Logs : les sorties sont redirigées vers logs/nba-collector.log et
# logs/nba-evaluator.log dans le projet (persistants, contrairement à /tmp).
# Voir les logs : tail -50 logs/nba-collector.log
#
# Budget : 6 créneaux × 3 crédits = 18 crédits/jour max (saison WNBA).
# Avec les collectes conditionnelles, la consommation réelle est bien moindre
# (skip hors-saison et jours sans matchs). Projection mensuelle : ~438 crédits
# en pic (saison), ~393 avec la garde de réserve (voir CLAUDE.md).
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
LOG_DIR="$PROJECT_DIR/logs"
MARKER_START="# nba-odds-tracker (1.7) — début"
MARKER_END="# nba-odds-tracker (1.7) — fin"

echo "Installation des jobs cron pour NBA Odds Tracker..."
echo "Répertoire projet : $PROJECT_DIR"

# Crée le dossier des logs s'il n'existe pas (persistant, contrairement à /tmp).
mkdir -p "$LOG_DIR"

# Supprime d'anciens jobs marqués (idempotent) : du marqueur de début au marqueur de fin.
if crontab -l 2>/dev/null | grep -q "$MARKER_START"; then
    crontab -l 2>/dev/null | sed "/$MARKER_START/,/$MARKER_END/d" > /tmp/nba_cron_new
else
    crontab -l 2>/dev/null > /tmp/nba_cron_new || true
fi

# Ajoute les nouveaux jobs.
# `docker compose run --rm` : exécute le one-shot puis supprime le conteneur.
# `--no-deps` : ne démarre pas le listener (déjà en route via `docker compose up -d`).
# Logs redirigés vers logs/nba-*.log (persistants dans le projet, gitignored).
cat >> /tmp/nba_cron_new <<EOF
$MARKER_START
# Collecte du matin (découverte + cotes d'ouverture) — INCONDITIONNELLE (--morning)
0 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector python -m collector --morning >> $LOG_DIR/nba-collector.log 2>&1
# Collecte de l'après-midi (relevé intermédiaire) — conditionnelle
0 15 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Collecte H-6 (tip-offs WNBA ~02:00 Paris) — conditionnelle
0 20 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Collecte H-3 (tip-offs WNBA ~02:00 Paris) — conditionnelle
0 23 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Collecte H-1 bloc 1 (fenêtre de décision, tip-offs 01:00–03:00 Paris) — conditionnelle
0 1 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Collecte H-1 bloc 2 (côte Ouest, tip-offs 03:00–04:45 Paris) — conditionnelle
45 2 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Évaluateur (bilan du matin + rapport hebdo le lundi)
30 9 * * * cd $PROJECT_DIR && docker compose run --rm --no-deps evaluator >> $LOG_DIR/nba-evaluator.log 2>&1
$MARKER_END
EOF

crontab /tmp/nba_cron_new
rm /tmp/nba_cron_new

echo ""
echo "Jobs cron installés :"
crontab -l | sed -n "/$MARKER_START/,/$MARKER_END/p"
echo ""
echo "Logs des collectes  : tail -50 $LOG_DIR/nba-collector.log"
echo "Logs de l'évaluateur: tail -50 $LOG_DIR/nba-evaluator.log"
echo ""

# ─── Détection des lignes étrangères au projet ───
# Parcourt le crontab final et signale toute ligne active (non commentée, non vide)
# qui n'est pas dans le bloc nba-odds-tracker. N'efface rien automatiquement (trop
# risqué) : affiche un avertissement pour que l'utilisateur vérifie manuellement.
echo "Vérification des lignes étrangères au projet..."
FOREIGN_LINES=$(crontab -l 2>/dev/null | awk '
    /# nba-odds-tracker \(1\.7\) — début/ { in_block = 1; next }
    /# nba-odds-tracker \(1\.7\) — fin/ { in_block = 0; next }
    !in_block && NF > 0 && $1 !~ /^#/ { print }
')
if [ -n "$FOREIGN_LINES" ]; then
    echo "⚠️  Lignes étrangères au projet détectées dans le crontab (hors bloc nba-odds-tracker) :"
    echo "$FOREIGN_LINES" | while IFS= read -r line; do
        echo "    $line"
    done
    echo ""
    echo "    Ces lignes ne sont pas gérées par ce script. Vérifie-les manuellement"
    echo "    et supprime-les avec 'crontab -e' si elles sont parasites."
else
    echo "✅ Aucune ligne étrangère détectée."
fi

echo ""
echo "Bot d'écoute : lance-le en continu avec :"
echo "  cd $PROJECT_DIR && docker compose up -d listener"
echo ""
echo "⚠️  Limite cron-WSL2 : le PC doit rester allumé (pas de veille)."
echo "    Cette limite motive le déploiement EC2 (phase 3)."
echo ""
echo "Pour désinstaller les jobs : crontab -l | grep -v 'nba-odds-tracker' | crontab -"