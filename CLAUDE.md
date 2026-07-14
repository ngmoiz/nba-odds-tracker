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
  decided_at TEXT
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
  verdict_won INTEGER,              -- 1/0 ; pour NO_BET : la sélection pressentie serait-elle passée ?
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
| R5 | **Cohérence croisée** | spread ET moneyline ET/OU total bougent de façon cohérente simultanément | +2 |
| R6 | **Divergence bookmaker** | un bookmaker s'écarte de ≥ 7 % (en probabilité) du consensus des autres | +2 → oriente vers `ANOMALIE` |
| R7 | **Incohérence spread/moneyline** | chez un même bookmaker, spread et moneyline racontent des histoires différentes | +2 → oriente vers `ANOMALIE` |

### 6.3 Alertes temps réel

Après chaque collecte : toute règle R1, R2 ou R4 déclenchée envoie immédiatement une alerte Telegram de type « info » (ce n'est **pas** une décision).

### 6.4 Décision finale (à la collecte H-1)

- `signal_score ≥ 6` **et** cohérence globale (pas de signaux contradictoires) → **`SIGNAL`** (avec sélection, marché, ligne, cote, justificatif).
- Règles R6/R7 dominantes → **`ANOMALIE`** (à vérifier manuellement, pas une recommandation).
- Sinon → **`NO_BET`** (défaut). Stocker quand même la sélection « pressentie » (celle au meilleur score partiel) pour l'évaluation des faux négatifs.

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
4. **Rapport hebdomadaire** (lundi matin) : taux de réussite des `SIGNAL`, performance **par règle déclenchante** et **par marché**, CLV moyen, performance des `NO_BET` pressentis (faux négatifs), et rappel du nombre d'évaluations cumulées.

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
7. docker-compose + cron WSL2.

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
| 2026-07-15 | Gestion des dépendances via `pyproject.toml` + `uv` | Outil moderne, rapide, standard émergent utile en DevOps ; alternative `pip`/`requirements.txt` écartée. |
| 2026-07-15 | Accès SQLite via le module standard `sqlite3` (pas d'ORM) | SQL brut plus lisible et pédagogique ; la couche `common/db` sera de toute façon réécrite pour DynamoDB en phase 4, une abstraction lourde maintenant serait du gâchis. |
| 2026-07-15 | Règle 0.4.2 clarifiée : append-only limité à `odds_snapshots` | Lever l'ambiguïté ; `matches`/`verdicts`/`evaluations`/`positions` ont un cycle de vie normal. |
| 2026-07-15 | Convention de modélisation `odds_snapshots` documentée (section 5) | `selection` = équipe ou `Over`/`Under` ; `line` = valeur de ligne pour `spreads`/`totals`, `NULL` pour `h2h`. |
| 2026-07-15 | **À TRANCHER (étape évaluateur)** : source des scores finaux | Le budget quota de The Odds API est serré (≈450/500 crédits pour les seules cotes) et l'endpoint `scores` consommerait la marge. Piste privilégiée : réserver The Odds API aux cotes et récupérer les résultats via une API NBA/WNBA gratuite dédiée. Options chiffrées à présenter à l'étape évaluateur. |
| 2026-07-14 | Création du document (V1) | — |