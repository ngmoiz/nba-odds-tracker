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
RUN uv sync --frozen --no-dev

# Copie le code source et les fichiers de configuration.
COPY src/ ./src/
COPY config.yaml scripts/ ./

# Point d'entrée : on lance Python via uv pour bénéficier de l'environnement
# géré par uv. La commande réelle (ex. `python -m collector`) est fournie
# par docker-compose.yml ou la ligne de commande docker.
ENTRYPOINT ["uv", "run"]
CMD ["python", "-m", "listener"]