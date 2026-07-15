# NBA Odds Tracker

Outil personnel et automatisé de **suivi de l'évolution des cotes NBA/WNBA** (pré-match).
Il enregistre les cotes de plusieurs bookmakers au fil du temps, détecte les
**mouvements significatifs** via un moteur de règles à seuils, et rend un verdict
par match : `NO_BET` (défaut) / `SIGNAL` / `ANOMALIE`.

> Le signal, ce n'est pas la cote : c'est **son mouvement** (direction, vitesse,
> synchronisation entre bookmakers, cohérence entre marchés). L'outil détecte des
> **anomalies de marché**, pas des « paris gagnants ». `NO_BET` est le verdict par défaut.

La source de vérité complète du projet (architecture, règles métier, roadmap) est
dans [CLAUDE.md](CLAUDE.md).

---

## État d'avancement (phase 1 — MVP local)

| Étape | Composant | État |
|---|---|---|
| 1.1 | Squelette, config, logging, base SQLite | ✅ |
| 1.2 | Client The Odds API + collecteur + machine à états | ✅ |
| 1.3 | Analyseur : moteur de règles R1–R7 + verdict | ✅ |
| 1.4 | Notificateur Telegram (envoi alertes + verdicts) | ✅ |
| 1.5 | Bot d'écoute (boutons Telegram → positions) | ✅ |
| 1.6 | Évaluateur (résultats, CLV, bilan quotidien + rapport hebdo) | ✅ |
| 1.7 | docker-compose + cron WSL2 | ✅ |

Aujourd'hui, le collecteur interroge l'API, enregistre les relevés, l'analyseur
écrit **alertes** et **verdicts** en base, le notificateur les **envoie sur
Telegram**, et le bot d'écoute **enregistre tes décisions** (prise / passe) quand tu
cliques sur les boutons d'un verdict.

---

## Prérequis

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** (gestionnaire de paquets/environnements) :
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source $HOME/.local/bin/env
  ```
- Une **clé API [The Odds API](https://the-odds-api.com)** (plan gratuit : 500 crédits/mois).

---

## Installation

```bash
# 1. Installer les dépendances (crée le .venv automatiquement)
uv sync

# 2. Créer le fichier de secrets à partir du modèle, puis le remplir
cp .env.example .env
#    -> renseigner au minimum ODDS_API_KEY dans .env

# 3. Initialiser la base SQLite (tables, index, triggers append-only)
uv run python scripts/init_db.py
```

> ⚠️ Le fichier `.env` contient tes secrets : il est ignoré par Git, ne le commite jamais.
> `.env.example` (versionné) ne sert qu'à documenter les variables attendues.

---

## Configuration

Deux fichiers, deux rôles :

- **[config.yaml](config.yaml)** — paramètres non secrets, versionnés : sport, marchés,
  **seuils des règles R1–R7**, planning de collecte, budget quota, seuil de décision.
  Tous les seuils sont ici, jamais codés en dur.
- **`.env`** — secrets et réglages machine :

  | Variable | Rôle |
  |---|---|
  | `ODDS_API_KEY` | Clé The Odds API — cotes (obligatoire) |
  | `BALLDONTLIE_API_KEY` | Clé balldontlie — scores finaux (évaluateur) |
  | `TELEGRAM_BOT_TOKEN` | Token du bot (fourni par @BotFather) |
  | `TELEGRAM_CHAT_ID` | Conversation cible des messages |
  | `DATABASE_PATH` | Chemin du fichier SQLite (défaut `./data/nba_odds.db`) |
  | `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Commandes

```bash
# Lancer une collecte + analyse + envoi Telegram (sport par défaut = NBA)
uv run python -m collector

# Forcer un autre sport SANS toucher config.yaml (utile hors-saison NBA)
uv run python -m collector --sport basketball_wnba

# Rejouer uniquement l'envoi Telegram des alertes/verdicts en attente
# (utile après un incident réseau ; sans effet si Telegram n'est pas configuré)
uv run python -m notifier

# Démarrer le bot d'écoute (processus qui tourne en continu) : il enregistre
# tes clics sur les boutons des verdicts. À laisser tourner en fond.
uv run python -m listener

# Évaluer les matchs clos de la veille (scores balldontlie, CLV) + bilan Telegram.
# À lancer chaque matin (cron).
uv run python -m evaluator

# Initialiser / réinitialiser la base (idempotent)
uv run python scripts/init_db.py

# Lancer toute la suite de tests
uv run pytest

# Lancer le lint
uv run ruff check src/ tests/
```

**Astuce** — pour tester sans polluer ta base principale, redirige la base vers un
fichier jetable le temps d'une commande :

```bash
DATABASE_PATH=/tmp/essai.db uv run python -m collector --sport basketball_wnba
```

---

## Docker (étape 1.7)

L'outil est conteneurisé : une **image commune** pour tous les composants, avec un
point d'entrée paramétré. Le bot d'écoute tourne en continu, les collecteurs et
l'évaluateur sont lancés par cron.

### Lancer le bot d'écoute (continu)

```bash
# Construit l'image et démarre le listener en arrière-plan
docker compose up -d listener

# Voir les logs
docker compose logs -f listener

# Arrêter
docker compose down
```

### Lancer une collecte manuellement (one-shot)

```bash
docker compose run --rm collector
# ou avec un sport différent :
docker compose run --rm collector python -m collector --sport basketball_wnba
```

### Lancer l'évaluateur manuellement (one-shot)

```bash
docker compose run --rm evaluator
```

### Installer le cron WSL2

Le script `scripts/setup_cron.sh` installe les jobs cron pour les collecteurs
(5 collectes/jour) et l'évaluateur (matin + rapport hebdo le lundi) :

```bash
chmod +x scripts/setup_cron.sh
./scripts/setup_cron.sh
```

Planning (heure locale Europe/Paris) :

| Heure | Job |
|---|---|
| 09:00 | Collecte du matin (découverte + cotes d'ouverture) |
| 09:30 | Évaluateur (bilan du matin + rapport hebdo le lundi) |
| 15:00 | Collecte après-midi (relevé intermédiaire) |
| 18:00 | Collecte H-6 |
| 21:00 | Collecte H-3 |
| 23:00 | Collecte H-1 (fenêtre de décision) |

Pour désinstaller les jobs : `crontab -l | grep -v 'nba-odds-tracker' | crontab -`

---

## Comprendre ce que produit l'outil

### Cycle de vie d'un match (machine à états)

```
DECOUVERT ──▶ SUIVI ──▶ DECIDE ──▶ CLOS ──▶ EVALUE
```

- **DECOUVERT** : match vu pour la première fois + cotes d'ouverture (référence).
- **SUIVI** : relevés suivants, analyse après chaque collecte.
- **DECIDE** : verdict final figé (dans la fenêtre avant le tip-off).
- **CLOS** : tip-off atteint, plus de collecte.
- **EVALUE** : résultat récupéré le lendemain (étape 1.6).

### Le moteur de règles

Chaque règle déclenchée ajoute des points à un **score de signal**. À partir de 6,
et en l'absence d'anomalie, le verdict devient `SIGNAL`.

| # | Règle | Points |
|---|---|---|
| R1 | Mouvement de ligne spread (≥ 2 pt depuis l'ouverture) | +3 |
| R2 | Steam move (variation de proba ≥ 5 % en ≤ 3 h) | +3 |
| R3 | Tendance soutenue (≥ 3 relevés même sens) | +2 |
| R4 | Synchronisation multi-bookmakers (≥ 4 books) | +3 |
| R5 | Cohérence croisée spread ↔ moneyline (confirmation) | +2 |
| R6 | Divergence bookmaker (≥ 7 % du consensus) | +2 → `ANOMALIE` |
| R7 | Incohérence spread/moneyline chez un book | +2 → `ANOMALIE` |

Les seuils sont configurables dans [config.yaml](config.yaml).

### Verdicts

- **`SIGNAL`** : score ≥ 6 et cohérence globale.
- **`ANOMALIE`** : incohérence de marché détectée (R6/R7) — à vérifier manuellement.
- **`NO_BET`** : défaut. La sélection « pressentie » est quand même stockée pour
  mesurer les faux négatifs.

Le verdict est **re-décidé à chaque collecte** tant que le match est proche du tip-off
(décision « à H-1 », sur les données les plus fraîches). S'il change **matériellement**
(type ou sélection), l'ancien message Telegram est édité (« remplacé ») et un nouveau
est envoyé ; un signal qui retombe en `NO_BET` déclenche une **annulation**. Dès que tu
prends une position, le verdict est **gelé**. Un clic sur un message périmé est rejeté.

### Notifications Telegram

Après chaque collecte, le notificateur pousse sur Telegram ce que l'analyseur vient
d'écrire en base :

- **alertes temps réel** (R1/R2/R4) : règle déclenchée + détail du mouvement ;
- **verdicts `SIGNAL` et `ANOMALIE`** : justificatif complet (drapeau R6 inclus s'il
  s'est déclenché) + boutons `✅ Je me positionne` / `➖ Je passe`.

Les `NO_BET` restent en base (pour l'évaluation des faux négatifs) mais **ne sont pas
envoyés** — évite un flux quotidien de « rien à signaler ». Les types de verdict
notifiés sont configurables dans [config.yaml](config.yaml) (`notifier.verdicts_notified`).
La base sert de file d'attente : chaque ligne envoyée est horodatée (`notified_at`),
un envoi échoué reste en attente et repart au passage suivant.

### Prises de position (bot d'écoute)

Le bot d'écoute (`python -m listener`) tourne **en continu** et écoute tes clics sur
les boutons d'un verdict :

- `✅ Je me positionne` → décision `take` ; `➖ Je passe` → décision `pass`.
- Dans **les deux cas**, on enregistre la **cote médiane du dernier relevé** au moment
  du clic (`odds_at_click`) : « passer » est une décision évaluable, distincte de « ne
  pas réagir ». L'évaluateur pourra ainsi comparer plus tard le résultat (et le CLV) de
  tes prises **et** de tes passes.
- **Premier clic gagnant** : une seule décision par verdict, toutes actions confondues.
- Seuls les clics venant de ta conversation (`TELEGRAM_CHAT_ID`) sont acceptés.

Ces décisions personnelles sont **indépendantes** de l'auto-évaluation du modèle :
l'évaluateur note **tous** les verdicts contre les résultats réels — y compris les
`NO_BET` et même si tu ne cliques jamais — pour mesurer la performance du modèle. Les
deux axes se rejoignent seulement dans les bilans.

### Évaluation et CLV (bilan du matin)

Chaque matin, l'évaluateur (`python -m evaluator`) :

- récupère les **scores officiels** de la veille via [balldontlie](https://www.balldontlie.io)
  (gratuit, NBA+WNBA), apparie chaque match par équipes + date (aucun crédit The Odds API) ;
- calcule pour chaque verdict s'il aurait **gagné / perdu / push** (le push — remboursement —
  est exclu du taux de réussite), y compris pour les `NO_BET` (faux négatifs) ;
- calcule le **CLV** (Closing Line Value) = proba dé-marginée de clôture − proba au verdict :
  positif = on a battu la ligne de clôture ;
- écrit tout dans `evaluations`, passe le match en `EVALUE`, et envoie le **bilan** Telegram.

⚠️ Garde-fou (règle 11) : tant que **50–100 évaluations** ne sont pas cumulées, les taux
sont du bruit statistique — aucun seuil ne doit être modifié. Le bilan le rappelle
explicitement en dessous du seuil.

---

## Structure du projet

```
nba-odds-tracker/
├── config.yaml            # seuils des règles, planning, quota
├── .env / .env.example    # secrets (jamais commités) / modèle
├── scripts/init_db.py     # initialisation de la base
├── src/
│   ├── common/            # config, logging, base SQLite, client API
│   ├── collector/         # collecte + machine à états
│   ├── analyzer/          # prétraitement, règles, scoring, verdict
│   ├── notifier/          # envoi Telegram (client + formatage + file d'attente)
│   ├── listener/          # bot d'écoute (clics → positions ; polling)
│   └── evaluator/         # évaluation des verdicts (résultats, grading, CLV, bilan)
└── tests/                 # pytest (priorité au moteur de règles)
```

---

## Quota API

- Une collecte complète (h2h + spreads + totals, région `us`) = **3 crédits**.
- Une requête qui ne renvoie aucun match (hors-saison) = **0 crédit**.
- L'endpoint « scores » (évaluateur) coûte **2 crédits** par appel.
- Le quota restant est **loggé après chaque appel** (`x-requests-remaining`).

Budget cible : ~5 collectes/jour ≈ 450 crédits/mois, sur les 500 du plan gratuit.

---

## Développement

Les données sont testées **sans consommer de quota** grâce à des relevés simulés
([tests/fixtures.py](tests/fixtures.py)) : chaque règle a son scénario déterministe.
Le moteur de règles est le composant le plus couvert (tests obligatoires).
