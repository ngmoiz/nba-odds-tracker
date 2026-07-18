#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# setup_cron.sh — Installe les jobs cron WSL2 pour le NBA Odds Tracker.
#
# Planning (architecture auto-ordonnancée, Lot 2) :
#
# Un SEUL battement `*/20` (toutes les 20 min). Le collecteur décide lui-même,
# à chaque tick, quoi collecter — plus de crons par créneau :
#
#   Tick collecteur : */20 * * * *  → auto-ordonnancement (voir ci-dessous)
#   Évaluateur      : 09:30         → bilan du matin (+ rapport hebdo le lundi)
#
# À chaque tick, `run_collection` :
#   - fait la collecte du matin si on est dans la fenêtre du matin (~09:00 UTC),
#     une seule fois par jour (idempotence via meta['daily_morning_collected_...']) ;
#   - groupe les matchs actifs en vagues (tip-offs espacés de ≤ 45 min) ;
#   - sert les cibles dues de chaque vague, calées sur le tip-off le plus précoce :
#     H-6, H-3, verdict (H-2), re-décision (H-1), et clôture (H-0.4, per-match) ;
#   - ne consomme AUCUN crédit si aucune cible n'est due (skip), ou si l'API ne
#     renvoie aucun match (hors-saison / jour sans match).
# Les 6 cibles, leurs marchés et priorités sont dans config.yaml (collector.targets).
#
# Garde de réserve : si le quota restant passe sous quota.reserve (config.yaml),
# les cibles priorité 2-3 (matin/H-6/H-3) sont sautées + notification Telegram
# (dédupliquée). Les cibles priorité 1 (verdict/re-décision/clôture) passent
# toujours. La collecte du matin rafraîchit le quota et lève la garde au reset.
#
# Logs : les sorties sont redirigées vers logs/nba-collector.log et
# logs/nba-evaluator.log dans le projet (persistants, contrairement à /tmp).
# Voir les logs : tail -50 logs/nba-collector.log
#
# Budget (selon le nombre de vagues W et de matchs M par jour) :
#   crédits/jour ≈ 3 (matin) + 12·W (4 cibles de vague × 3 marchés)
#                            + ~1·M (clôture, 1 marché de verdict/match).
# Ex. : 1 vague / 3 matchs ≈ 18/jour ; 2 vagues / 6 matchs ≈ 33 ; 3 vagues / 9 ≈ 48.
# Jours sans match ≈ 0 (ticks en skip). Voir README.md et CLAUDE.md.
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
# Tick collecteur auto-ordonnancé (Lot 2) : le collecteur décide lui-même quoi
# collecter (matin, vagues H-6/H-3/verdict/re-décision, clôture per-match).
# Un seul battement toutes les 20 min ; la plupart des ticks ne consomment rien.
*/20 * * * * cd $PROJECT_DIR && docker compose run --rm --no-deps collector >> $LOG_DIR/nba-collector.log 2>&1
# Évaluateur (bilan du matin + rapport hebdo le lundi) — calé sur l'horloge, pas sur les tip-offs
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