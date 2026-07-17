# ─────────────────────────────────────────────────────────────
# Image commune — NBA Odds Tracker
#
# Tous les composants (collecteur, analyseur, notificateur, bot d'écoute,
# évaluateur) partagent le même code et les mêmes dépendances. On construit
# une **image unique** avec un point d'entrée paramétré : le composant à
# lancer est choisi via la commande docker-compose, pas via un Dockerfile
# par composant.
#
# Justification (section 8 du CLAUDE.md) : une image par composant
# dupliquerait ~300 Mo de couches Python identiques pour ne varier que la
# dernière ligne de commande. Une image commune est plus rapide à construire,
# plus simple à maintenir, et cohérente avec le fait que les composants
# s'enchaînent dans le même processus (collecteur → analyseur → notificateur).
# ─────────────────────────────────────────────────────────────

FROM python:3.12-slim

# uv : gestionnaire d'environnement rapide et reproductible.
# On l'installe en binaire statique (pas de virtualenv dédié à uv lui-même).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Répertoire de travail dans le conteneur.
WORKDIR /app

# Installe les dépendances système minimales (aucune ici en slim, mais on
# garde le pattern pour d'éventuelles extensions C futures).
# tzdata : fuseaux horaires pour l'affichage Europe/Paris (cohérent avec la
# dépendance Python `tzdata`, redondance volontaire pour les logs conteneur).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copie d'abord les fichiers de dépendances (cache Docker : cette couche
# n'est invalidée que si pyproject.toml ou uv.lock changent, pas à chaque
# modification de code source).
COPY pyproject.toml uv.lock ./

# Installe les dépendances en mode production (sans le groupe dev).
# `--frozen` : respecte strictement uv.lock (reproductibilité CI/Docker/EC2).
# `--no-dev` : exclut pytest/ruff (non nécessaires en production).
# `--no-install-project` : n'installe pas le package local (les sources ne sont
# pas encore copiées) — installe uniquement les dépendances externes. Cette
# couche est cacheable et n'est invalidée que si pyproject.toml/uv.lock changent.
RUN uv sync --frozen --no-dev --no-install-project

# Copie le code source et les fichiers de configuration.
COPY src/ ./src/
COPY config.yaml scripts/ ./

# Installe le package local (nba-odds-tracker) dans le .venv. Les dépendances
# externes sont déjà en cache (étape précédente) — cette couche est rapide et
# sans réseau. Elle est invalidée à chaque modification de code source, ce qui
# est attendu. Sans cette étape, `uv run` re-synchroniserait à chaque démarrage
# (M1, revue externe) — téléchargement réseau + délai à chaque cron.
RUN uv sync --frozen --no-dev

# Point d'entrée : on lance Python via uv pour bénéficier de l'environnement
# géré par uv. La commande réelle (ex. `python -m collector`) est fournie
# par docker-compose.yml ou la ligne de commande docker.
# `--no-sync` (M1, revue externe) : les dépendances sont déjà installées au build
# (`uv sync --frozen --no-dev`). Sans ce drapeau, `uv run` re-synchronise les deps
# (dont le groupe dev) à chaque démarrage — téléchargement réseau + délai à chaque
# cron. `--no-sync` supprime cette dépendance réseau au runtime.
ENTRYPOINT ["uv", "run", "--no-sync"]
CMD ["python", "-m", "listener"]