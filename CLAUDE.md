# NBA Odds Tracker — Document racine du projet

> **Ce document est la source de vérité du projet.** Claude Code doit le lire intégralement avant toute action et s'y conformer. En cas d'ambiguïté, poser la question au développeur plutôt que de supposer.

---

## 0. Consignes pour Claude Code (à lire en premier)

### 0.1 Rôle et posture

- **Phases 1 et 2 (développement local, tests, CI)** : Claude Code **implémente** le code, mais explique systématiquement ses choix techniques (bibliothèque, structure, pattern) en quelques phrases avant ou après chaque implémentation significative.
- **Phases 3 et 4 (infrastructure, déploiement AWS)** : Claude Code **ne fait PAS à la place du développeur**. Il agit en **mentor** : il explique le concept, justifie le choix de l'outil, donne les étapes à réaliser, puis laisse le développeur exécuter lui-même (console AWS, CLI, SSH). Il vérifie ensuite le résultat et débogue avec lui. Objectif : montée en compétences DevOps réelle, pas un déploiement presse-bouton.

### 0.2 Contexte du développeur

- QA Automation Engineer (~3 ans d'expérience) en transition vers le DevOps / Platform Engineering.
- Bon niveau Python, Docker, CI/CD, tests automatisés (Robot Framework, Playwright, pytest).
- **Débutant** en Kubernetes, AWS et architecture cloud : tout concept infra doit être expliqué depuis la base, avec définition des termes techniques à leur première apparition.
- Environnement de dev : **WSL2 (Ubuntu) + VS Code** sous Windows.

### 0.3 Style pédagogique attendu

- Définir chaque terme technique à sa première utilisation.
- Privilégier les analogies concrètes et les exemples pas à pas.
- Terminer chaque explication de concept important par un court paragraphe **« À retenir »** : l'essentiel + ce qu'on rencontre souvent en pratique.
- Français formel.

### 0.4 Règles de développement non négociables

1. **Aucun secret dans le code ni dans Git** : clés API et tokens uniquement via variables d'environnement (fichier `.env` local, jamais commité — `.gitignore` dès le premier commit). Fournir un `.env.example` documenté.
2. **Base de données en append-only pour l'historique des cotes** : la règle append-only s'applique **uniquement à la table `odds_snapshots`** (les relevés de cotes ne sont jamais modifiés ni supprimés, seulement ajoutés). Les autres tables ont un cycle de vie normal : `matches.status` évolue au fil de la machine à états, et `verdicts`/`evaluations`/`positions` reçoivent des écritures classiques.
3. **Logs structurés** dans chaque composant (horodatage, composant, niveau, message). Utiliser le module `logging` de Python, pas de `print`.
4. **Tests unitaires obligatoires** sur le moteur de règles (composant le plus critique) avec `pytest`.
5. **Pas de scraping** des sites de bookmakers. Source de données : API uniquement.
6. **Économie de quota API** : chaque appel à The Odds API doit être justifié ; respecter le budget défini en section 4.3.
7. Tous les seuils de détection sont **configurables** dans un fichier de configuration (`config.yaml` ou équivalent), jamais codés en dur.
8. Code commenté en français, noms de variables/fonctions en anglais (convention).

### 0.5 Amélioration continue — le document est vivant

Ce document décrit la V1, pas une vérité définitive. Claude Code est
explicitement encouragé à :

1. **Donner un avis critique** quand une décision de ce document lui semble
   sous-optimale au vu du code ou des données réelles — le dire ouvertement,
   proposer une alternative argumentée, et laisser le développeur trancher.
2. **Proposer des améliorations à partir des premiers résultats** : dès que
   des évaluations réelles s'accumulent (alertes trop nombreuses ou trop
   rares, règles qui ne se déclenchent jamais, seuils manifestement mal
   réglés, faux positifs récurrents), le signaler spontanément avec les
   chiffres à l'appui.
3. **Respecter les garde-fous** : les propositions de modification de seuils
   restent soumises à la règle des 50–100 évaluations minimum (section 11).
   En dessous, Claude Code peut constater une tendance mais doit recommander
   d'attendre avant d'agir.
4. **Mettre à jour ce document** : toute évolution validée avec le
   développeur (seuil modifié, règle ajoutée/retirée, changement
   d'architecture) doit être répercutée dans CLAUDE.md dans la même session,
   avec une ligne dans un journal des décisions en fin de document
   (date, changement, justification).

Ce qui reste non négociable même en itérant : les règles de développement
de la section 0.4, le verdict NO_BET par défaut, et la posture mentor sur
les phases 3–4.
---

## 1. Vision et objectif du produit

Outil personnel et automatisé de **suivi de l'évolution des cotes NBA** (pré-match uniquement) qui :

1. Découvre chaque jour les matchs NBA à venir et enregistre leurs cotes d'ouverture.
2. Suit l'évolution des cotes sur plusieurs jours avec une fréquence adaptative (intensifiée dans les dernières heures avant le match).
3. Détecte les mouvements significatifs (steam moves, tendances, incohérences entre marchés, divergences entre bookmakers) via un **moteur de règles à seuils** (pas de machine learning en V1).
4. Rend une **décision finale par match à H-1** : `NO_BET` (défaut) / `SIGNAL` / `ANOMALIE`.
5. Notifie tout via un **bot Telegram interactif** (alertes temps réel, verdict avec boutons de prise de position, bilans).
6. **Évalue automatiquement le lendemain** toutes ses décisions contre les résultats réels, y compris les `NO_BET`, pour mesurer et calibrer la performance du modèle indépendamment des positions du développeur.

### 1.1 Principes métier fondamentaux

- **Le signal, ce n'est pas la cote : c'est son mouvement** (direction, vitesse, synchronisation entre bookmakers, cohérence entre marchés).
- **`NO_BET` est le verdict par défaut.** Un `SIGNAL` exige un mouvement net, synchronisé et cohérent. L'outil détecte des anomalies de marché, pas des « paris gagnants ».
- Les cotes incluent la **marge du bookmaker** : toute conversion en probabilité doit retirer la marge (probabilité implicite = 1/cote, puis normalisation pour que la somme des issues fasse 100 %).
- En NBA, le signal principal est le **mouvement de la ligne du spread** (ex. -7.5 → -4.5), plus que le mouvement de la cote elle-même (qui reste ~1.90 des deux côtés).

### 1.2 Hors périmètre (V1)

- ❌ Live betting (cotes en direct pendant le match) — latence rédhibitoire pour un outil automatisé.
- ❌ Player props (paris joueurs) — peu liquides, mal couverts par les API gratuites, volume de données ×20.
- ❌ Machine learning — règles à seuils uniquement, déboguables et explicables.
- ❌ Placement automatique de paris — l'outil informe, l'humain décide.
- ❌ Autres sports — extension possible en V2 (l'architecture doit néanmoins rester générique : le sport est un paramètre, pas une constante).

---

## 2. Architecture générale

Cinq composants Python découplés (principe de responsabilité unique), partageant une base SQLite :

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│  COLLECTEUR │────▶│   SQLITE    │◀────│  ANALYSEUR   │
│ (cron)      │     │ (historique)│     │ (post-       │
└─────────────┘     └─────────────┘     │  collecte)   │
                          ▲  ▲          └──────┬───────┘
┌─────────────┐           │  │                 │
│ ÉVALUATEUR  │───────────┘  │          ┌──────▼───────┐
│ (cron matin)│              │          │ NOTIFICATEUR │──▶ Telegram (envoi)
└─────────────┘              │          └──────────────┘
                             │
                    ┌────────┴────────┐
                    │  BOT D'ÉCOUTE   │◀── Telegram (réception,
                    │ (long-running)  │     boutons inline)
                    └─────────────────┘
```

| Composant | Déclenchement | Responsabilité |
|---|---|---|
| **Collecteur** | cron (fréquence adaptative) | Interroger The Odds API, insérer les relevés en base, gérer le cycle de vie des matchs |
| **Analyseur** | immédiatement après chaque collecte | Appliquer les règles de détection, calculer le score de signal, produire alertes et verdicts |
| **Notificateur** | appelé par l'analyseur et l'évaluateur | Envoyer les messages Telegram (alertes, verdicts avec boutons, bilans) |
| **Bot d'écoute** | processus continu | Recevoir les clics sur boutons inline (callback queries), enregistrer les prises de position |
| **Évaluateur** | cron chaque matin | Récupérer les scores finaux, évaluer tous les verdicts de la veille, calculer les métriques, envoyer le bilan |

En local : le collecteur/analyseur/évaluateur sont déclenchés par **cron** (WSL2) ; le bot d'écoute tourne en continu (mode **polling** de Telegram). Le tout est conteneurisé (voir section 8).

---

## 3. Cycle de vie d'un match (machine à états)

```
DECOUVERT ──▶ SUIVI ──▶ DECIDE ──▶ CLOS ──▶ EVALUE
```

| Statut | Entrée dans le statut | Ce que fait l'outil |
|---|---|---|
| `DECOUVERT` | Découverte lors de la collecte du matin | Enregistrement du match + **cotes d'ouverture** (référence de tous les mouvements) |
| `SUIVI` | Dès le 2ᵉ relevé | Relevés selon le planning adaptatif, analyse après chaque relevé, alertes temps réel si règle déclenchée |
| `DECIDE` | Analyse de la collecte H-1 | Verdict final calculé et envoyé sur Telegram avec justificatif + boutons de position |
| `CLOS` | Heure de tip-off atteinte | Plus aucune collecte pour ce match |
| `EVALUE` | Job évaluateur du lendemain | Résultat réel récupéré, verdict évalué, métriques mises à jour |

---

## 4. Source de données : The Odds API

### 4.1 Références

- Site : https://the-odds-api.com — documentation officielle à consulter par Claude Code avant d'écrire le client API.
- Sport : `basketball_nba`. Région : `us`. Format de cotes : décimal.
- Marchés V1 : `h2h` (moneyline), `spreads` (handicap), `totals` (over/under).
- Endpoint scores pour les résultats finaux (utilisé par l'évaluateur).

### 4.2 Notions clés du quota

- Plan gratuit : **500 crédits/mois**.
- Coût d'une requête = **nombre de marchés × nombre de régions** (indépendant du nombre de matchs retournés). Une collecte complète h2h+spreads+totals sur `us` = **3 crédits**.
- Une seule requête renvoie **tous les matchs NBA à venir** avec les cotes de plusieurs bookmakers : on récupère toujours tout, on ne demande jamais match par match.

### 4.3 Budget quota et planning de collecte (configurable)

Budget cible : ~5 collectes complètes/jour ≈ 15 crédits/jour ≈ 450 crédits/mois (+ marge pour l'endpoint scores).

Planning quotidien par défaut (heure locale Europe/Paris ; les matchs NBA se jouent la nuit en heure française — le planning doit être calé sur les heures de tip-off, pas sur des heures fixes naïves) :

1. **Collecte du matin** : découverte des nouveaux matchs + relevé (= cotes d'ouverture pour les nouveaux).
2. **Collecte d'après-midi** : relevé intermédiaire.
3. **Collectes H-6, H-3, H-1** avant le premier bloc de tip-offs de la « journée NBA » : c'est la fenêtre où entre l'essentiel de l'argent informé (annonces de blessures, load management, compos).

Le collecteur calcule lui-même, à chaque exécution, quels matchs sont dans quelle fenêtre. Prévoir un compteur de crédits consommés (The Odds API renvoie les crédits restants dans les en-têtes de réponse : les logger à chaque appel).

---

## 5. Modèle de données (SQLite)

SQLite = base de données dans un simple fichier, sans serveur. Fichier stocké dans un volume Docker partagé entre composants. Schéma indicatif (Claude Code peut l'affiner, en expliquant ses choix) :

**Convention de modélisation des relevés (`odds_snapshots`)** : la colonne `selection` contient l'équipe concernée pour `h2h` et `spreads`, et la valeur `Over`/`Under` pour `totals`. La colonne `line` contient la valeur de la ligne (`-7.5`, `224.5`) pour `spreads` et `totals`, et vaut `NULL` pour `h2h` (le moneyline n'a pas de ligne).

```sql
-- Matchs et leur cycle de vie
matches (
  match_id TEXT PRIMARY KEY,        -- id fourni par The Odds API
  sport TEXT,                       -- 'basketball_nba' (générique pour V2)
  home_team TEXT, away_team TEXT,
  tipoff_utc TEXT,                  -- toujours stocker en UTC, convertir à l'affichage
  status TEXT,                      -- DECOUVERT / SUIVI / DECIDE / CLOS / EVALUE
  created_at TEXT
)

-- Historique brut des cotes : APPEND-ONLY, jamais de UPDATE/DELETE
odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT, bookmaker TEXT,
  market TEXT,                      -- h2h / spreads / totals
  selection TEXT,                   -- équipe, ou Over/Under
  line REAL,                        -- la ligne (-7.5, 224.5) ; NULL pour h2h  ⚠ colonne dédiée : en NBA le signal est dans le mouvement de la ligne
  odds REAL,                        -- la cote décimale
  snapshot_at TEXT                  -- timestamp UTC du relevé
)

-- Alertes temps réel émises pendant le suivi
alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT, rule TEXT, details TEXT, created_at TEXT
)

-- Verdicts finaux (décision H-1)
verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT,
  verdict TEXT,                     -- NO_BET / SIGNAL / ANOMALIE
  selection TEXT,                   -- sélection concernée (NULL si NO_BET sans pressenti)
  market TEXT, line REAL,
  odds_at_verdict REAL,             -- cote au moment du verdict (base du CLV)
  signal_score INTEGER,
  rules_triggered TEXT,             -- liste des règles ayant contribué (JSON)
  rationale TEXT,                   -- justificatif lisible envoyé sur Telegram
  decided_at TEXT,                  -- horodatage de la (dernière) décision ; avance à chaque re-décision H-1
  logic_version INTEGER,            -- version de la logique de décision (1 = pré-fix H-1, 2 = re-décision) ; pour ségréguer les stats
  telegram_message_id INTEGER,      -- id du message Telegram courant (anti-clic sur message périmé)
  superseded_message_id INTEGER     -- id d'un message rendu obsolète par une re-décision matérielle (à éditer)
)

-- Prises de position du développeur (via boutons Telegram)
positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verdict_id INTEGER,
  odds_at_click REAL,               -- cote au moment du clic (celle qui compte)
  clicked_at TEXT
)

-- Évaluations du lendemain
evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verdict_id INTEGER,
  home_score INTEGER, away_score INTEGER,
  outcome TEXT,                     -- état EXPLICITE 'won'/'lost'/'push' (jamais un NULL métier) ; 'push' remboursé, hors dénominateur du taux ; pour NO_BET : issue qu'aurait eue la sélection pressentie
  closing_odds REAL,                -- cote de clôture (dernier snapshot avant tip-off)
  clv REAL,                         -- Closing Line Value : odds_at_verdict vs closing_odds
  evaluated_at TEXT
)
```

Tous les timestamps en **UTC** en base ; conversion en Europe/Paris uniquement à l'affichage (messages Telegram).

---

## 6. Moteur de règles de détection

Toutes les valeurs ci-dessous sont des **défauts configurables** dans `config.yaml`.

### 6.1 Prétraitement

- Calcul de la **probabilité implicite dé-margée** pour chaque cote : `p = 1/cote`, puis normalisation par marché et par bookmaker pour que la somme des issues = 100 %.
- Référence de tous les mouvements : le **relevé d'ouverture** (premier snapshot du match).

### 6.2 Règles (chacune ajoute des points au score de signal du match/sélection)

| # | Règle | Condition par défaut | Points |
|---|---|---|---|
| R1 | **Mouvement de ligne spread** | \|ligne actuelle − ligne d'ouverture\| ≥ 2.0 points | +3 |
| R2 | **Steam move** | variation de probabilité implicite ≥ 5 % en ≤ 3 h sur un marché | +3 |
| R3 | **Tendance soutenue** | mouvement dans la même direction sur ≥ 3 relevés consécutifs | +2 |
| R4 | **Synchronisation multi-bookmakers** | ≥ 4 bookmakers sur 5 bougent dans le même sens sur la même fenêtre | +3 |
| R5 | **Cohérence croisée** (confirmation) | depuis l'ouverture, le spread du consensus bouge de ≥ 1.0 pt **et** la proba moneyline dé-margée du consensus varie de ≥ 3 %, **dans le même sens** | +2 |
| R6 | **Divergence bookmaker** | un bookmaker s'écarte de ≥ 7 % (en probabilité) du consensus des autres | +2 → oriente vers `ANOMALIE` |
| R7 | **Incohérence spread/moneyline** (V1 : contradiction de favori) | chez un même bookmaker, le favori selon le spread contredit le favori selon le moneyline, avec écart de proba moneyline ≥ 3 % **et** \|spread\| ≥ 1.5 | +2 → oriente vers `ANOMALIE` |

> **Notes de formalisation (R5, R7).** R5 est une règle de *confirmation* : ses seuils sont volontairement plus hauts que le bruit d'équilibrage des books (0.5 pt / 2 % déclencheraient presque partout et videraient de son sens le seuil de verdict). R1 et R5 partagent le même calcul « mouvement du spread consensus depuis l'ouverture » (R1 seul dès 2.0 pt ; R5 dès 1.0 pt **si** le moneyline confirme). R7 en V1 ne détecte que les contradictions autour d'un spread proche de zéro ; l'évolution V1.1 (comparaison de la proba moneyline à la proba implicite du spread via Φ(spread/σ), σ configurable par ligue) est notée au journal des décisions.

### 6.3 Alertes temps réel

Après chaque collecte : toute règle R1, R2 ou R4 déclenchée envoie immédiatement une alerte Telegram de type « info » (ce n'est **pas** une décision).

### 6.4 Décision finale (à la collecte H-1)

Le **score de signal** ne compte que les points des règles de **mouvement (R1–R5)** ; les points d'anomalie (R6/R7) orientent le verdict mais ne gonflent pas le score. Arbitrage, dans l'ordre :

1. **R7** déclenchée (contradiction spread/moneyline chez un book) → **`ANOMALIE`** : la cohérence globale est cassée, un tel dossier ne peut pas être un `SIGNAL` propre.
2. Sinon, **score de mouvement ≥ 6** → **`SIGNAL`** (avec sélection, **marché déclencheur**, ligne, cote, justificatif). Si **R6** s'est aussi déclenchée, elle devient un **drapeau** joint au signal (« divergence bookmaker signalée ») — elle ne masque pas un signal fort.
3. Sinon, **R6** seule → **`ANOMALIE`** (divergence à vérifier, sans signal de mouvement derrière).
4. Sinon → **`NO_BET`** (défaut). Stocker quand même la sélection « pressentie » pour l'évaluation des faux négatifs.

La **cote de référence du verdict** (`odds_at_verdict`, base du CLV) est celle du **marché qui a déclenché le signal** : le spread quand R1/R5 sont en jeu (cas dominant en basket), sinon le moneyline.

**Re-décision jusqu'au tip-off (« vraiment à H-1 »).** Le verdict n'est pas figé à la première collecte de la fenêtre : il est **re-évalué à chaque collecte** tant que le match est dans la fenêtre, la ligne `verdicts` étant mise à jour en place (`decided_at` avance). Deux garde-fous : (1) dès qu'une **position** est prise, le verdict est **gelé** (le développeur s'est engagé) ; (2) un changement **matériel** (type de verdict ou sélection) rend l'ancien message Telegram obsolète — il est **édité** (« remplacé ») et un nouveau message est envoyé (« mis à jour »), voire une **annulation** si un signal retombe en `NO_BET`. On ne laisse jamais une invitation à se positionner devenue caduque. Chaque verdict porte une `logic_version` (1 = pré-correctif figé trop tôt, 2 = re-décision H-1) pour distinguer les cohortes en calibration.

---

## 7. Bot Telegram

### 7.1 Bibliothèque et mode

- `python-telegram-bot` (version stable actuelle — vérifier la doc).
- **Envoi** : requêtes HTTP simples (notificateur).
- **Réception** : mode **polling** en local (aucune config réseau) ; le **webhook** sera introduit en phase 4 (AWS). Définir les deux termes au développeur au moment de l'implémentation.
- Token du bot et chat_id en variables d'environnement.

### 7.2 Messages

1. **Alerte temps réel** (pendant le suivi) : match, règle déclenchée, détail du mouvement, heure.
2. **Verdict H-1** : verdict + justificatif complet (« spread Boston passé de -7.5 à -5, baisse chez 4 books sur 5 entre H-6 et H-3, moneyline cohérente ») + **boutons inline** : `✅ Je me positionne` / `➖ Je passe`. Un clic sur « Je me positionne » enregistre la position avec la **cote au moment du clic**. Règle d'or : ne jamais faire taper ce qui peut être cliqué.
3. **Bilan du matin** (évaluateur) : résultats de la veille, verdicts gagnants/perdants, résultat des positions personnelles.
4. **Rapport hebdomadaire** (lundi matin, en plus du bilan quotidien) : taux de réussite des `SIGNAL` **par marché** et **par règle déclenchante** (multi-comptage assumé — un signal porté par R1+R5 est compté dans chaque règle), CLV moyen, performance des `NO_BET` pressentis (faux négatifs), et rappel du nombre d'évaluations cumulées. Période glissante de 7 jours sur `evaluated_at` (aucun trou entre deux rapports). **Segregation `logic_version`** : les verdicts pré-correction H-1 (v1) et propres (v2) sont agrégés dans des blocs séparés ; une cohorte vide est omise. Le garde-fou règle 11 (50 évaluations minimum) se mesure sur la **cohorte de calibration** (v2), pas sur le cumul global — affichage dual (« X cumulées, dont Y en logique v2 »). Parsing défensif **non silencieux** : un `rules_triggered` illisible est loggé en warning (avec `verdict_id`) et une mention « ⚠️ N verdict(s) à règles illisibles » apparaît dans le rapport. Pure agrégation par-dessus `evaluations` — aucune nouvelle donnée produite.

---

## 8. Stack technique et structure du projet

- **Python 3.11+**, `httpx` ou `requests` pour l'API, `python-telegram-bot`, `pyyaml` pour la config, `pytest` pour les tests.
- **SQLite** (module standard `sqlite3` ou SQLAlchemy Core — Claude Code choisit et justifie).
- **Docker** : une image par composant (ou une image commune avec point d'entrée paramétré — choisir et justifier). **docker-compose** pour l'orchestration locale, avec volume partagé pour la base et fichier `.env`.
- Structure indicative :

```
nba-odds-tracker/
├── CLAUDE.md                  # ce document
├── README.md                  # doc utilisateur : installation, lancement, config
├── config.yaml                # seuils des règles, planning, budget quota
├── .env.example               # variables attendues, documentées
├── docker-compose.yml
├── src/
│   ├── collector/
│   ├── analyzer/
│   ├── notifier/
│   ├── listener/              # bot d'écoute
│   ├── evaluator/
│   └── common/                # accès DB, modèles, logging, client API
├── tests/                     # pytest — priorité au moteur de règles
└── scripts/                   # utilitaires (init DB, seed de test, etc.)
```

- Prévoir un **jeu de données de test** (snapshots simulés) pour développer et tester l'analyseur **sans consommer de quota API**.

---

## 9. Roadmap

### Phase 1 — MVP local (Claude Code implémente + explique)

1. Squelette du projet, config, logging, init de la base.
2. Client The Odds API + collecteur + machine à états des matchs.
3. Moteur de règles + score de signal + verdicts (développé d'abord sur données simulées).
4. Notificateur Telegram (envoi simple).
5. Bot d'écoute (polling + boutons inline + table positions).
6. Évaluateur (scores, évaluations, CLV, bilans).
7. docker-compose + cron WSL2. ✅ **Image Docker commune** (point d'entrée paramétré, `uv sync --frozen --no-dev`), `docker-compose.yml` (listener en continu + collector/évaluateur one-shot, volume nommé `nba-data`), `scripts/setup_cron.sh` (5 collectes/jour + évaluateur 09:30, heure Europe/Paris).

**Critère de sortie de phase : l'outil tourne 7 jours en local sans intervention, les alertes et bilans Telegram arrivent correctement.** Ne pas passer à la phase 2 avant.

### Phase 2 — Qualité et CI (Claude Code implémente + explique)

1. Couverture pytest du moteur de règles et de l'évaluateur (cas nominaux + cas limites : ligne qui traverse zéro, match reporté, bookmaker manquant).
2. Dépôt GitHub propre (`.gitignore`, README, pas de secrets).
3. GitHub Actions : lint (`ruff`), tests, build des images Docker à chaque push. Expliquer chaque étape du workflow YAML.

### Phase 3 — Déploiement EC2 (Claude Code GUIDE, le développeur EXÉCUTE)

Mode mentor obligatoire : expliquer chaque concept (VPC, groupe de sécurité, paire de clés SSH, IAM, free tier), donner les étapes, laisser le développeur les réaliser dans la console/CLI AWS, vérifier avec lui.

1. Création du compte/IAM, budget alert AWS (éviter les mauvaises surprises de facturation).
2. Lancement d'une instance `t3.micro` (free tier), connexion SSH, installation Docker.
3. Transfert du projet (git clone), `.env` sur la machine, lancement docker-compose, cron système.
4. Supervision basique : consultation des logs à distance, redémarrage automatique des conteneurs (`restart: unless-stopped`).

### Phase 4 — Serverless (Claude Code GUIDE, le développeur EXÉCUTE)

1. Concepts : Lambda, EventBridge Scheduler (le « cron du cloud »), DynamoDB, IAM roles — expliqués depuis zéro.
2. Migration du stockage SQLite → DynamoDB (une Lambda n'a pas de disque persistant) : refonte de la couche `common/db`, expliquée pas à pas.
3. Collecteur/analyseur/évaluateur en Lambdas planifiées ; bot d'écoute en webhook (API Gateway → Lambda).
4. Comparaison finale EC2 vs serverless (coûts, exploitation, limites) — question d'entretien DevOps classique à préparer avec le développeur.

---

## 10. Définition de « terminé » (Definition of Done) par fonctionnalité

Une fonctionnalité est terminée quand : le code est testé (pytest vert), loggé, configuré via `config.yaml`/`.env` (rien en dur), documenté dans le README, et **expliquée au développeur** (il doit pouvoir la décrire avec ses mots).

## 11. Garde-fous métier (à rappeler dans les messages Telegram si pertinent)

- L'outil détecte des **anomalies de marché**, pas des paris gagnants. `NO_BET` est le défaut.
- **Aucun seuil de détection ne doit être modifié avant 50–100 évaluations cumulées** : en dessous, les taux de réussite sont du bruit statistique.
- La métrique de qualité de long terme est le **CLV** (cote au verdict vs cote de clôture), plus fiable que le taux de victoire brut.

## 12. Journal des décisions

| Date | Changement | Justification |
|---|---|---|
| 2026-07-16 | **Étape 1.7** : conteneurisation + cron WSL2. **Image Docker commune** (pas une image par composant) avec point d'entrée paramétré (`ENTRYPOINT ["uv","run"]`, commande via docker-compose). `Dockerfile` : `python:3.12-slim` + `uv` (binary copy from ghcr.io), `uv sync --frozen --no-dev` (production sans pytest/ruff), cache Docker optimisé (dépendances copiées avant le code source). `.dockerignore` exclut tests/data/secrets. `docker-compose.yml` : listener en continu (`restart: unless-stopped`) + collector/évaluateur one-shot (lancés par cron via `docker compose run --rm --no-deps`), volume nommé `nba-data` pour la base SQLite. `scripts/setup_cron.sh` : 5 collectes/jour (09:00, 15:00, 18:00, 21:00, 23:00) + évaluateur 09:30 (heure Europe/Paris), idempotent (marqueur `# nba-odds-tracker (1.7)`), logs via `logger -t nba-*`. | Image commune plutôt qu'une par composant : évite de dupliquer ~300 Mo de couches Python identiques pour ne varier que la commande ; cohérent avec l'enchaînement collecteur→analyseur→notificateur dans le même processus. `--no-dev` : pytest/ruff non nécessaires en production. `--frozen` : reproductibilité stricte (uv.lock). Volume nommé : la base SQLite persiste entre les one-shots et le listener. |
| 2026-07-16 | **Correctifs rapport hebdo (post-revue)** : (1) garde-fou règle 11 mesuré sur la **cohorte de calibration** (v2), pas le cumul global — affichage dual (« X cumulées, dont Y en logique v2 »), test 55 cumul dont 15 v2 → garde-fou actif. (2) Parsing défensif de `rules_triggered` **non silencieux** : warning loggé avec `verdict_id`, mention « ⚠️ N verdict(s) à règles illisibles » dans le rapport quand N > 0. | Les évaluations v1 (pré-correction) ne doivent pas faire basculer le seuil prématurément ; un JSON illisible ne doit pas être avalé sans trace. |
| 2026-07-16 | **Amélioration candidate (rattrapage hebdo, non bloquante)** : l'envoi conditionné au lundi seul (`weekday == 0`) saute entièrement si le cron du lundi échoue. Piste : envoyer le rapport si le dernier envoi remonte à > 7 jours (mécanisme de rattrapage), pas seulement si `weekday == 0`. Nécessite de persister la date du dernier envoi (table de métadonnées ou colonne dédiée). | Robustesse : un échec ponctuel ne doit pas faire perdre une semaine entière de calibration. |
| 2026-07-15 | **Rapport hebdomadaire (post-1.6)** : pure agrégation par-dessus `evaluations` (aucune nouvelle donnée produite). Période glissante de 7 jours sur `evaluated_at` (aucun trou entre deux rapports). Taux de réussite des SIGNAL **par marché** et **par règle déclenchante** (multi-comptage assumé — un signal porté par R1+R5 est compté dans chaque règle, avec note explicite), CLV moyen (moyenne des `clv` non-None), performance des NO_BET pressentis (faux négatifs). **Segregation `logic_version`** : v1 (pré-correction H-1) et v2 (décision H-1) agrégés dans des blocs séparés, cohorte vide omise. Garde-fou règle 11 sur la **cohorte v2** (pas le cumul global). Envoi le lundi matin **en plus** du bilan quotidien via `notifier.direct`. Module pur `weekly.py` (agrégation + formatage) + fonctions `db.get_weekly_signal_evals`/`get_weekly_nobet_evals`/`count_evaluations_by_logic_version` ; 18 tests dédiés. | Clôturer le périmètre 1.6 (rapport différé à l'étape évaluateur) ; ségréger les cohortes pour ne pas polluer la calibration avec les verdicts pré-fix H-1 ; réutiliser le helper `success_rate` (pushes hors dénominateur) et le notificateur existant. |
| 2026-07-15 | Gestion des dépendances via `pyproject.toml` + `uv` | Outil moderne, rapide, standard émergent utile en DevOps ; alternative `pip`/`requirements.txt` écartée. |
| 2026-07-15 | Accès SQLite via le module standard `sqlite3` (pas d'ORM) | SQL brut plus lisible et pédagogique ; la couche `common/db` sera de toute façon réécrite pour DynamoDB en phase 4, une abstraction lourde maintenant serait du gâchis. |
| 2026-07-15 | Règle 0.4.2 clarifiée : append-only limité à `odds_snapshots` | Lever l'ambiguïté ; `matches`/`verdicts`/`evaluations`/`positions` ont un cycle de vie normal. |
| 2026-07-15 | Convention de modélisation `odds_snapshots` documentée (section 5) | `selection` = équipe ou `Over`/`Under` ; `line` = valeur de ligne pour `spreads`/`totals`, `NULL` pour `h2h`. |
| 2026-07-15 | **À TRANCHER (étape évaluateur)** : source des scores finaux | Le budget quota de The Odds API est serré (≈450/500 crédits pour les seules cotes) et l'endpoint `scores` consommerait la marge. Piste privilégiée : réserver The Odds API aux cotes et récupérer les résultats via une API NBA/WNBA gratuite dédiée. Options chiffrées à présenter à l'étape évaluateur. |
| 2026-07-15 | R5 formalisée : spread consensus ≥ 1.0 pt **et** Δproba moneyline dé-margée consensus ≥ 3 %, même sens, depuis l'ouverture | Règle de confirmation : des seuils bas (0.5 pt / 2 %) se déclencheraient sur le bruit d'équilibrage des books et gonfleraient tous les scores, vidant de son sens le seuil de verdict à 6. |
| 2026-07-15 | R7 V1 = contradiction de favori + garde-fous (écart proba ≥ 3 % **et** \|spread\| ≥ 1.5) | Peu de faux positifs, adapté à un déclencheur d'ANOMALIE. **Limite connue** : aveugle hors des matchs serrés. **Évolution V1.1** : comparer la proba moneyline à la proba implicite du spread via Φ(spread/σ), σ configurable par ligue (~11.5 NBA, à ajuster WNBA). |
| 2026-07-15 | Consensus = **médiane** entre bookmakers | Robuste aux cotes aberrantes/périmées, contrairement à la moyenne. **Piste V2** : consensus pondéré par les *sharp books* (Pinnacle/Circa) qui mènent le marché — Pinnacle est en région `eu` (coût quota supplémentaire à arbitrer) ; le schéma stocke déjà le `bookmaker` par relevé, donc rien n'est perdu. |
| 2026-07-15 | V1 : le verdict est enregistré sur le marché **h2h** (moneyline) de l'équipe pressentie | Fournit une cote concrète et quotable pour le CLV, même quand le signal vient du spread (les détails spread restent dans le `rationale`). **Évolution possible** : verdict porté sur le marché déclencheur (spread) avec sa ligne. |
| 2026-07-15 | **Corrigé (revue temps 2)** : `odds_at_verdict` enregistrée sur le **marché déclencheur** (spread si R1/R5, sinon h2h) | Le CLV compare la cote au verdict à la cote de clôture du *même* marché ; enregistrer la cote h2h pour un signal spread mesurait le CLV du mauvais marché. Le verdict stocke désormais marché + ligne + cote médiane du marché signalé. |
| 2026-07-15 | **Corrigé (revue temps 2)** : arbitrage anomalie/score = option 2 | Une anomalie ne prime plus en absolu. R7 (contradiction) reste bloquant ; un score de mouvement ≥ seuil donne SIGNAL même si R6 s'est déclenchée (R6 → drapeau) ; R6 seule sans signal → ANOMALIE. Les points R6/R7 sont exclus du score de mouvement (ils n'inflent plus le score). |
| 2026-07-15 | **Corrigé (post-revue)** : `favored_selection(data, driving_market)` suit désormais le marché déclencheur | La sélection est dérivée du sens du mouvement de spread quand le signal est spread (R1/R5), sinon de la hausse de proba h2h, sinon du favori courant. Corrige le verdict *directionnellement* faux (« Miami +6 » à rebours de la steam Boston -6). Vérifié : le cas reproduit sort désormais « Home -5.0 » ; test de régression ajouté. |
| 2026-07-15 | **Dette identifiée (revue temps 4)** : consensus médian calculé sur un panel de books variable | Le nombre de books change entre relevés (API live) ; la médiane peut bouger par effet de composition → R1/R2/R3/R5 faussées, sans mouvement réel. Aucun test ne couvre un panel changeant. **Piste** : mesurer les mouvements sur les books communs aux deux instants, ou pondérer/filtrer. |
| 2026-07-15 | **Dette identifiée (revue temps 4)** : le verdict est figé à la 1ʳᵉ collecte de la fenêtre, pas à H-1 | `analyze_match` décide dès l'entrée dans la fenêtre de 1,5 h → verdict possiblement arrêté trop tôt, sur données plus anciennes, à rebours de l'intention « décision à H-1 ». **Piste** : décider à la collecte la plus proche du tip-off, ou re-décider tant que dans la fenêtre. |
| 2026-07-15 | **Dettes mineures (revue temps 4)** : alertes non dédupliquées ; marché `totals` quasi inanalysé | Alertes : une règle persistante réémet à chaque collecte (spam Telegram à gérer en 1.4). Totals : R1 ne couvre que `spreads`, R3/R4 traitent les totals via la proba (plate) → mouvement de ligne totals invisible. |
| 2026-07-15 | **Corrigé (dette H-1)** : le verdict n'est plus figé à la 1ʳᵉ collecte de la fenêtre → **re-décision à chaque collecte** jusqu'au tip-off (mise à jour en place, `decided_at` avance). **Gel** dès qu'une position est prise ; changement **matériel** (type/sélection) → l'ancien message Telegram est **édité** (« remplacé ») + nouveau message (« mis à jour »), et un signal retombant en `NO_BET` déclenche une **annulation** (malgré NO_BET-silencieux). **Anti-clic-périmé** : le listener rejette un clic dont le `message_id` ≠ `verdicts.telegram_message_id` (aucune position). Supersession robuste : `superseded_message_id` jamais écrasé par NULL (COALESCE, protège une double re-décision), effacé **seulement après** une édition réussie (at-least-once). `logic_version` (1/2) ajoutée pour **distinguer les cohortes** en calibration (les évaluations pré-fix sont identifiables). | Respecter l'intention « décision à H-1 » (données les plus fraîches) ; ne jamais laisser sur le téléphone une invitation à parier caduque ; ne pas polluer la calibration avec des verdicts mal datés. |
| 2026-07-15 | **Amélioration candidate (revue H-1, non bloquante)** : le message édité lors d'une supersession affiche un texte générique (« 🔁 Remplacé par une décision plus récente »). Il pourrait **conserver un rappel de l'ancien contenu** (ex. verdict/sélection d'origine barrés) pour la traçabilité côté lecteur. Nécessiterait de mémoriser le texte envoyé (ou de le reconstruire) — arbitrage stockage/lisibilité à faire. | Validé en démo réelle ; suffisant en l'état, l'anti-clic-périmé garantit déjà la sécurité. |
| 2026-07-15 | **Résolu** : dette « verdict figé à la 1ʳᵉ collecte de la fenêtre, pas à H-1 » (revue temps 4) → re-décision + supersession ci-dessus. | Était la dette à corriger impérativement avant l'automatisation (1.7) ; faite avant le rapport hebdo à la demande du développeur (chaque jour de retard accumulait des évaluations mal datées). |
| 2026-07-15 | **Corrigé (revue 1.6)** : push encodé en **état explicite** `outcome` ∈ {'won','lost','push'} au lieu de `verdict_won` NULL. | Un NULL ne doit jamais porter de sens métier (ambigu avec « non évalué »/« inconnu ») ; l'ancien schéma confondait aussi « push » et « sélection non notable ». `outcome` sépare proprement les deux (non notable = `None`, non inséré). Taux de réussite = `won / (won + lost)`, pushes hors dénominateur (helper `success_rate` testé). Migration par reconstruction de table (SQLite 3.34 sans `DROP COLUMN`) : `1→won`, `0→lost`, `NULL→push`. La commodité SQL (`AVG` ignore les NULL) ne justifiait pas de sacrifier l'explicite. |
| 2026-07-15 | **Étape 1.6** : évaluateur (moteur d'évaluation + bilan quotidien ; rapport hebdo différé). **Source des scores tranchée = balldontlie** (voie B : gratuit, NBA+WNBA, zéro crédit The Odds API). Réconciliation match Odds ↔ résultat par noms d'équipes normalisés + date calendaire US (±1 j). **Grading** : `verdict_won` 1/0 par marché (h2h/spreads/totals), **push → NULL** (remboursement exclu du taux) ; évalue aussi les `NO_BET` pressentis (faux négatifs). **CLV = proba dé-marginée de clôture − proba dé-marginée au verdict** (consensus médian ; cohérent règle 1.1). Cote de clôture = médiane du dernier relevé avant tip-off. Abandon d'un match sans résultat au-delà de `lookback_days` (évite le rescan infini ; gère le match reporté). Modules purs `grading`/`reconcile`/`clv` testés en priorité (cas limites : ligne traversant zéro, push, match reporté, sélection non cotée). Client `results_api_client` séparé ; envoi du bilan via `notifier.direct` réutilisant le `TelegramClient` de 1.4. | Fidèle à « The Odds API réservé aux cotes » ; auto-évaluation du modèle indépendante des positions ; CLV rigoureux (marge retirée). |
| 2026-07-15 | **Résolu** : entrée « À TRANCHER (source des scores finaux) » du 2026-07-15 → **balldontlie** retenu (voie B). | Le budget The Odds API (~450/500) ne supportait pas l'endpoint `scores` (~60 crédits/mois) ; balldontlie est gratuit et couvre NBA+WNBA. |
| 2026-07-15 | **Étape 1.5** : bot d'écoute Telegram (polling, `python-telegram-bot`). Enregistre les décisions humaines dans `positions`. **`odds_at_click` = option 1** : médiane du dernier relevé en base au moment du clic (zéro quota), repli sur `odds_at_verdict` si le marché/sélection n'est pas coté au dernier instant. **« Je passe » = option B aménagée** : colonne `positions.action` (`take`/`pass`), cote médiane enregistrée **dans les deux cas** → l'évaluateur pourra comparer résultat + CLV des prises ET des passes (« passer » est une décision évaluable, distincte de « ne pas réagir »). **Idempotence** : premier clic gagnant, toutes actions confondues (croisements pos↔skip testés). Sécurité : seuls les clics du `TELEGRAM_CHAT_ID` autorisé sont acceptés. Logique métier isolée dans des modules purs (`callbacks`/`odds`/`positions`), testée sans Telegram ; glue PTB (async, polling) minimale. | Refermer la boucle des boutons envoyés en 1.4 ; capturer le jugement humain comme donnée mesurable face au modèle. L'auto-évaluation du modèle (tous verdicts, y compris NO_BET) reste indépendante des positions (étape 1.6). |
| 2026-07-15 | **Étape 1.4 (ajout)** : traduction FR de la sélection au format bookmaker français (style Betclic) dans `formatting.py`. Ligne principale traduite, notation US conservée en ligne secondaire, virgule décimale partout, mention explicite « cote = médiane des books US, pas une cote FR ». Règles : spread -X,5 → « gagne de (X+1)+ » / côté + → « ne perd pas ou perd de X max » ; ligne entière -X,0 → idem + « (remboursé si écart = X) » (push) ; h2h → « Vainqueur du match (prolongations incluses) » ; totals → « +/- de X,5 points dans le match ». Fonction pure `traduire_selection` + tests dédiés (`test_formatting.py`), dont les deux côtés du spread et le cas ligne entière. **Limite** : le côté + d'une ligne **entière** n'était pas spécifié au cahier des charges → formulation déduite par symétrie (« ne perd pas ou perd de (X-1) max (remboursé si écart = X) »). **Périmètre tranché : traduction sur les verdicts uniquement** — une alerte décrit un *mouvement de marché* (contenu numérique, pas un pari à traduire), un verdict formule un *pari*. **Interdit** : parser le `details` texte libre des alertes pour en extraire la sélection (fragile par construction, casse au moindre changement de wording des règles). **Évolution candidate (si l'usage montre que les alertes sont dures à lire)** : enrichir la table `alerts` de colonnes `selection`/`market`/`line` remplies par l'analyseur, puis réutiliser `traduire_selection` — nécessite de toucher `db.py` + `analyzer.py`. | Rendre les décisions (paris) lisibles pour un parieur habitué aux books FR, sans laisser croire que la cote affichée est une cote FR ; les alertes restent des signaux de mouvement bruts. |
| 2026-07-15 | **Étape 1.4** : notificateur Telegram (envoi alertes + verdicts SIGNAL/ANOMALIE). Idempotence via colonne `notified_at` (file d'attente en base) ; envoi httpx simple (pas de `python-telegram-bot`, réservé à la réception en 1.5) ; boutons inline envoyés dès maintenant, `callback_data=pos:{id}`/`skip:{id}`, **clic traité en 1.5** (boutons inertes d'ici là) ; NO_BET non envoyé (`notifier.verdicts_notified` configurable) mais conservé pour l'évaluation ; drapeau R6 déjà rédigé par l'analyseur dans `rationale`, rendu tel quel. Dépendance `tzdata` ajoutée (affichage Europe/Paris hors OS pourvu). | Découplage analyseur (écrit) / notificateur (lit et envoie) ; livraison « au moins une fois » avec commit par message pour limiter les doublons. |
| 2026-07-14 | Création du document (V1) | — |