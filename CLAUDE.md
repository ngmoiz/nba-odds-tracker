# NBA Odds Tracker — Document racine du projet

> **Ce document est la source de vérité du projet.** Claude Code doit le lire intégralement avant toute action et s'y conformer. En cas d'ambiguïté, poser la question au développeur plutôt que de supposer.

---

## Invariants

> **Note** : Cette section doit rester synchronisée avec `.clinerules` (section "Décisions verrouillées").

**Décisions qui ne se rediscutent jamais :**

1. **Re-décision H-1** : implémentée et branchée par C1 (correctif revue externe 2026-07-17) — les matchs DECIDE sont réanalysés à chaque collecte tant qu'ils sont dans la fenêtre, le verdict est mis à jour en place jusqu'au tip-off ou à la prise de position ; une re-décision **matérielle** (changement de type de verdict ou de sélection) rend l'ancien message Telegram obsolète — il est édité (« remplacé ») et un nouveau message envoyé (« mis à jour »), voire annulé si un signal retombe en NO_BET.
2. **Déduplication** : `(match_id, target_name)` pour collectes (Lot 2, correction bug conception) ; `state_key` (market/selection|signe|ampleur) pour alertes.
3. **Cibles de collecte** : tip-off le plus précoce de la **vague** (bloc de tip-offs rapprochés), pas de la journée NBA entière, sauf closing per-match (dernier snapshot avant chaque tip-off).
4. **`window_hours = 2.5`** : verdict à H-2 (H-3 hors fenêtre, H-2 dedans), re-décision à H-1.
5. **`None` explicite obligatoire** : une donnée absente est représentée par `None` explicite, jamais par une valeur par défaut qui la masque. Bug d'origine : un score `0-0` (valeur par défaut) gradé « push » au lieu d'être traité comme résultat absent.
6. **Échec bruyant obligatoire** : jamais de no-op silencieux, tout échec loggé (warning min), anomalie parsing → mention rapport.
7. **Append-only sur `odds_snapshots`** : on invalide (statut, `superseded_message_id`), on ne supprime jamais.
8. **TEST_MODE jamais en production** : uniquement pour tests, détecté et rejeté au démarrage.
9. **Rituel de déploiement** : `docker compose build` + `up -d --force-recreate` + vérification (logs/base/Telegram).

---

## Définition de terminé

> **Note** : Cette section doit rester synchronisée avec `.clinerules` (section "Définition de terminé").

**Une tâche est terminée quand :**

1. **Suite complète pytest relancée avec total réel** (jamais addition de sous-ensembles).
2. **`ruff check` passe** (aucune violation non justifiée).
3. **`git status` propre ou commit effectué**.
4. **Preuve observée pour toute affirmation factuelle** (sortie commande collée) — **« déjà » interdit sans extrait code**.
5. **Test qui casse → question « la garantie tient-elle ? »** (jamais champ compatibilité sans justification).
6. **`scripts/check.sh` lancé avant rapport fin de lot**, sortie collée.
7. **Interdiction de supprimer, skipper ou réécrire un test pour le faire passer.** Un test qui échoue se corrige côté code, ou son échec est signalé tel quel et laissé rouge.
8. **Si un bug est identifié en cours de tâche, il est signalé explicitement dans le rapport final, même non corrigé** — jamais contourné en silence.
9. **Un rapport final ne présente que la sortie brute de `pytest -q`.** Les formulations « X% des tests exécutables », « N tests fonctionnels », ou toute addition de sous-ensembles sont interdites.
10. **Manquer de temps ou de contexte n'autorise aucun raccourci** : la bonne réponse est de s'arrêter et de rapporter l'état réel.

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
- Référence de tous les mouvements : le **relevé d'ouverture** (premier snapshot du match). ⚠️ « Ouverture » = **première collecte de l'outil**, pas l'ouverture réelle du marché : des lignes ont pu bouger avant que l'outil ne commence à suivre le match (lookahead antérieur possible). La fenêtre de collecte du matin (09:00 Paris) capte les cotes disponibles à cet instant, qui peuvent déjà refléter des mouvements overnight. Ce biais est inhérent au planning de collecte et sera partiellement atténué en phase 3 (EC2 24/7, collecte plus précoce).

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

### Phase V1.1 — « De suiveur à prédictif » (post-validation 7 jours)

> Plan détaillé (analyse indépendante + tri des avis IA + spec technique complète) dans
> `draft/analyse-amelioration.md` (**non versionné** : brouillon de travail). Le présent bloc
> en fige la substance dans la source de vérité. Rien ici ne modifie de seuil avant les
> 50–100 évaluations de la règle 11.

**Diagnostic.** L'outil est un *détecteur de mouvement de marché* (line-following), pas un
prédicteur de résultat. La pièce manquante pour créer de la valeur (et non hériter de celle
des autres) est une **opinion chiffrée indépendante sur l'issue**, confrontée au prix.

**Phase A — hygiène (pendant/juste après J7, ne rien casser).**
- A1 discipline : paper only, flat 1u, **CLV = métrique n°1**.
- A2 : lancer `scripts/analyze_r4_distribution.py` **à J7** → choisir le plancher R4 **sur données**.
- A3 **kill-switch** : valeurs **définies maintenant** au journal (ex. après 50 SIGNAL v3, si CLV
  glissant < 0 **et** win rate < 52,4 % → `NO_BET` forcé), **code livré avec la release v3, pas
  pendant le gel**.

**Phase B — cœur (post-J7).**
- B1 plancher d'ampleur R4 (`min_move_line`/`min_move_prob`) — **bloqué jusqu'aux données J7**.
- B2 pondération progressive R1 (2/4/6 pt).
- B3 filtre anti value-trap « move refroidi » : **mesuré en collectes consécutives (≥ 2-3), jamais
  en heures**, et **inapplicable en fenêtre de décision H-1** (moves frais = les plus exploitables).
- B4 cooldown / anti-corrélation (1-2 expositions corrélées par soirée).
- B5 **modèle de force baseline (Elo + marge de victoire + avantage terrain + repos/B2B) → `p_model`** ;
  décision **hybride** : `SIGNAL` seulement si `sign(move)` cohérent **ET** `|p_model − p_mkt| > τ`.
  **Pré-requis backfill** : rejouer la **saison WNBA complète** via balldontlie (la base n'a qu'~1
  semaine à mi-saison ; démarrer à 1500 rendrait `p_model` faux et `min_games_for_edge` jamais
  satisfait). **Bloquant** : confirmer d'abord l'endpoint WNBA de balldontlie (`games_path` est NBA).

**Séquencement des versions de logique** (évite de multiplier les resets de cohorte) :
- **v3** = B1+B2+B3+B4 + code du kill-switch A3 → 1er J0.
- **B5 en shadow** (edge loggé, non bloquant) = **aucun bump** (ne change aucune décision).
- **v4** = activation de la règle hybride bloquante → 2e J0.

**Multi-ligue (WNBA ↔ NBA).** `config.yaml` restructuré en `active_sport` + `defaults` + `leagues`
(merge profond dans `common/config.py`, forme plate préservée pour le reste du code). Base SQLite
**partagée**, partitionnée par `sport` ; toute agrégation de stats doit filtrer par `sport`.

**Phase C — raffinement (données à l'appui).** Ancrage sharp (Pinnacle `eu`, coût quota) ; totals
(mouvement de ligne réel + Poisson/normale) ; key numbers à la bascule NBA ; Kelly fractionnel
**seulement après CLV > 0 prouvé sur 200+ évals**.

**Statut permanent (lucidité).** Le CLV est mesuré sur la **médiane US loggée**, pas sur la cote
**exécutable** (Betclic) → il **surestime** l'edge encaissable. B5 ne corrige pas cet écart
(structurel). **Statut = laboratoire d'apprentissage** tant que CLV > 0 n'est pas prouvé sur
**≥ 100 évals en cohorte v4**.

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
| 2026-07-18 | **Bug snapshots post-tip-off (Lot 2) — garde de stockage autoritaire base** : le refactor Lot 2 (collecte par vague) a ajouté deux chemins de stockage — **collecte du matin** et **mode force** — qui itèrent *tous* les events de l'API et stockaient tout `commence_time > now` **sans consulter la base**. Un match déjà `CLOS` (tip-off d'origine passé) encore renvoyé par l'API avec un `commence_time` futur (reprogrammé, ou fenêtre live) était **re-stocké** → relevés post-tip-off (invariant 7 violé). Le chemin des vagues, lui, était déjà protégé par `close_finished_matches` (étape 2, avant le groupement : la vague ne contient jamais un match commencé). **Diagnostic par la simulation `simulate_lot2_complete.py`** : 3 matchs (m1/m2/m3) avec des snapshots à 08:12 (créneau matin) **sans entrée dans `collection_log`** → re-stockés par le chemin matin, pas par une cible. **Correctif** : helper unique `_event_tipoff_passed(conn, event, now)` — autorité = **base** (statut `CLOS` → exclu ; sinon tip-off DB atteint → exclu ; `commence_time` API utilisé seulement pour un match inconnu) — branché **avant tout stockage sur les 3 chemins** (matin, force, `_collect_and_record`). 2 tests de non-régression (matin + force) vérifiant les **deux moitiés** de la garantie : 0 relevé pour le match CLOS **et** collecte normale des autres matchs de la même exécution (une garde trop large casserait la seconde). Reproduction prouvée : tests rouges (`assert 6 == 0`) sur le code pré-correctif. | La garde `commence_time <= now` du code pré-Lot-2 reposait sur le tip-off **de l'API** ; en production l'API fige `commence_time`, mais un match reprogrammé ou re-listé en live défait cette garde. Le tip-off **DB** (figé à la découverte, cohérent avec `close_finished_matches`) est la seule autorité fiable ; un `CLOS` ne doit jamais recevoir de relevé quoi que renvoie l'API. |
| 2026-07-18 | **Défaut secondaire relevé (NON traité cette session)** : les chemins de collecte **matin** et **force** n'écrivent **rien dans `collection_log`** (seul `_collect_and_record` le fait). Conséquences : (1) leurs collectes sont **invisibles à l'audit** (c'est précisément ce qui a masqué le bug post-tip-off ci-dessus — les 6 snapshots re-stockés n'avaient aucune trace dans `collection_log`) ; (2) leurs crédits consommés sont **absents du comptage** basé sur `collection_log`. À traiter : journaliser matin et force dans `collection_log` (avec un `target_name` dédié, ex. `morning`/`force`) pour rendre toute collecte traçable et comptabilisée. | Une collecte qui ne laisse pas de trace fausse l'audit et le budget quota ; rendre les 3 chemins symétriques sur `collection_log`. Noté sans traiter (hors périmètre de la correction du bug post-tip-off). |
| 2026-07-17 | **Adoption du plan V1.1 « de suiveur à prédictif » (voir §9 Roadmap → Phase V1.1)** : analyse indépendante + tri de 3 avis IA (retenu/partiel/écarté) + un 4ᵉ avis d'amendement, consignés dans `draft/analyse-amelioration.md` (non versionné). **Retenu** : modèle de force Elo (marge de victoire + avantage terrain + repos/B2B) → `p_model`, décision hybride `SIGNAL ⟺ sign(move) cohérent ET |p_model−p_mkt|>τ` ; plancher d'ampleur R4 ; pondération progressive R1 ; filtre « move refroidi » ; kill-switch chiffré ; cooldown anti-corrélation ; discipline paper + CLV métrique n°1. **Écarté** (infaisable à cette échelle) : RLM (pas de données de splits sur l'API gratuite), injury reports temps réel automatisés, détection HFT. **4 amendements actés** : (a) backfill Elo = **saison WNBA complète** via balldontlie, pas la base locale (~1 semaine à mi-saison), pré-requis bloquant = confirmer l'endpoint WNBA (`games_path` actuel = NBA) ; (b) « move refroidi » mesuré en **collectes consécutives (≥2-3), jamais en heures**, inapplicable en fenêtre H-1 ; (c) **séquencement des versions** : v3 = B1→B4 + kill-switch (1er J0), B5 shadow **sans bump**, v4 = hybride bloquant (2ᵉ J0) ; (d) kill-switch **défini maintenant, codé en v3** (jamais pendant le gel). **Statut permanent** : le CLV est mesuré sur la médiane US loggée, pas sur la cote exécutable → surestime l'edge encaissable ; projet = **laboratoire** tant que CLV>0 n'est pas prouvé sur ≥100 évals cohorte v4. **1er chantier engagé (hors gel règle 11)** : refactor multi-ligue `config.yaml` (`active_sport`/`defaults`/`leagues`) + `load_config(sport=…)`. | Règle 0.5 (document vivant) : figer dans la source de vérité un plan prospectif qui vivait dans un fichier gitignoré, sans modifier aucun seuil avant les 50–100 évaluations (règle 11). La pièce manquante n'est pas une 8ᵉ règle de mouvement mais une opinion indépendante sur l'issue, confrontée au prix. |
| 2026-07-16 | **Correctif cron prev-validation 7 jours (post-1.7)** : planning 6 creneaux + collectes conditionnelles + garde de reserve + logs vers logs/ + ligne parasite supprimee + projection corrigee (evaluateur = balldontlie = 0 credit Odds API). Total mensuel : ~438 credits pic (saison WNBA), ~393 avec garde. Table meta ajoutee. Tests : skip base vide, collecte si matchs, morning inconditionnel, reserve, dedup, sortie de garde, borne window_hours=2.0. | Les tip-offs WNBA s etalent jusqu a 04:00 Paris (cote Ouest). Collectes conditionnelles economisent quota hors-saison. Garde de reserve protege fin de mois. /tmp perdu au reboot. Projection initiale comptait a tort 60 credits/mois pour evaluateur (balldontlie = gratuit). |
| 2026-07-16 | **Correctif planning cron (post-1.7)** : les collectes du soir sont recalées sur les fenêtres H-1 réelles des tip-offs WNBA (matchs en soirée heure US = 01:00–04:00 Paris). H-6 à 20:00, H-3 à 23:00, H-1 à 01:00 (au lieu de 18:00/21:00/23:00 qui rataient les tip-offs nocturnes). Sans ce correctif, la re-décision H-1 livrée à l'étape 1.6 ne s'exécutait jamais en production. **Limite structurelle cron-WSL2 documentée** : rien ne tourne si le PC est éteint ou en veille — motivation opérationnelle de la phase 3 (EC2, serveur 24/7). Pour la validation 7 jours, le PC restera allumé. | Les tip-offs WNBA sont nocturnes en heure de Paris ; un planning calé sur des heures « bureau » ne couvre jamais la fenêtre de décision. La limite WSL2 est inhérente au local et justifie le passage au cloud. |
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
| 2026-07-17 | **Enrichissement du formatage des mouvements (R1–R4)** : chaque alerte et justificatif de verdict affiche désormais le mouvement précis — direction explicite (📉 baisse / 📈 hausse sur la sélection observée), cote médiane avant → après, variation de probabilité dé-margée en **points de proba** (convention : points de proba, pas % de cote), référence temporelle (depuis l'ouverture pour R1/R3, fenêtre récente pour R2/R4), et conclusion « l'argent va vers [équipe] » pour h2h/spread (vers l'Over/Under pour totals). Logique **algébrique** pour spreads : `after.line < before.line` → argent vers cette équipe (renforcée), couvre favori, outsider et traversée de zéro. Cas stable (|δ| < ε) → « mouvement consensus négligeable » (pas de conclusion directionnelle sur un bruit). Aucun nouveau calcul de règle — les données existent dans les séries de consensus (`ConsensusPoint`), c'est du formatage via `_format_movement`. 5 tests dédiés (baisse favori, hausse h2h, totals, outsider s'affaiblit, traversée de zéro). | Lisibilité des alertes/verdicts : un mouvement décrit par « 📉 baisse, ligne -2,0 → -5,0, cote méd. 1,91 → 1,70, Δproba +5,2 pts (depuis l'ouverture), l'argent va vers Home » remplace l'ancien « 3,0 pt de mouvement ». Convention « points de proba » (×100, signé) plutôt que % de cote pour rester cohérent avec les seuils R2/R5 (définis en % de proba dé-margée). |
| 2026-07-17 | **Amélioration candidate (règle 11, non bloquante)** : pondération du score de signal par l'ampleur du mouvement. À étudier via le taux de réussite par tranche d'ampleur (ex. 2–4 pt, 4–6 pt, 6+ pt) une fois 50+ évaluations cumulées. **Ne rien scorer maintenant** — la règle 11 interdit toute modification de seuil/calibration avant 50–100 évaluations. L'ampleur est déjà disponible dans les `detail` enrichis ci-dessus. | Un mouvement de 2,0 pt (juste au seuil R1) et un de 6,0 pt portent le même score (+3) aujourd'hui ; si les grands mouvements réussissent significativement mieux, une pondération pourrait affiner le score. À réévaluer avec les données réelles. |
| 2026-07-17 | **Déduplication des alertes par changement d'état** : une règle persistante (R1/R2/R4) ne réémet plus à chaque collecte si l'état (sélection + direction) n'a pas changé depuis la dernière alerte. Colonne `state_key` ajoutée à `alerts` (migration idempotente) ; `RuleResult.state_key` calculé par `_state_key(market, selection, before, after)` (encode `market/selection|signe`). L'analyseur compare avec `db.get_last_alert_state` avant d'insérer. Si la direction s'inverse (baisse → hausse) ou la sélection change, une nouvelle alerte est émise. Résout la dette « alertes non dédupliquées » (revue temps 4). 2 tests dédiés (même état → pas de réémission ; changement de direction → nouvelle alerte). | Le spam Telegram (5 alertes R4 identiques sur Portland en 5 collectes) disparaît. La déduplication est par **état** (sélection + direction), pas par identité de texte — robuste aux changements de wording. |
| 2026-07-17 | **state_key enrichi d ampleur + format canonique machine** : la clé de déduplication encode désormais market/selection|signe|ampleur (point décimal, pas de formatage français). R1 = ligne arrondie, R2 = palier de proba %, R4 = nombre de books. Un changement d ampleur (approfondissement de ligne, renforcement de synchro 8→9 books) change la clé → nouvelle alerte avec évolution injectée ("8 → 9 bookmakers"). parse_state_key() assure le round-trip. | Divergence de spec reconnue : le state_key basique (signe seul) rendait silencieux exactement les évolutions qu on a décidé de garder. |
| 2026-07-17 | **Seuils métier quasi-stable (ε jumeaux)** : _EPS=1e-9 reste pour le bruit flottant (_direction) ; nouveaux seuils _NEGLIGIBLE_PROBA=0,2 pt et _NEGLIGIBLE_LINE=0,25 pt pour le formatage. Trois tiers : stable (< _EPS) / quasi stable (< seuil métier, ampleur affichée, pas de conclusion) / directionnel (≥ seuil). Configurables dans config.yaml. | Arbitrage : 1e-9 assumé (l ampleur est affichée, l utilisateur juge) vs ε métier (la conclusion "l argent va vers" n a de sens que pour un mouvement significatif). Retenu : ε métier — un mouvement de 0,1 pt n est pas de l argent informé. |
| 2026-07-17 | **Comportement oscillatoire de la dédup** : la dédup par state_key ré-alerte à chaque bascule (9→8→9 ré-alerte à chaque flip). Noté comme point à surveiller pendant la validation 7 jours — pas de mécanisme préventif avant données réelles. | Première observation chiffrée (mardi 16/07, Portland Fire) : séquence 8→7→8→9 books sur 4 collectes → 4 alertes (la 5e collecte à 7=7 est muette). C est de l information réelle (sortie puis retour d un book dans la synchro), pas du bruit — l hystérésis n est pas justifiée pour l instant. À surveiller : si le bruit oscillatoire génère trop d alertes, envisager un hystérésis ou un cooldown. |
| 2026-07-17 | **Bug snapshots post-tip-off (cotes live stockées)** : la simulation a révélé que 30 snapshots live à 00:45 (tip-off Portland 23:10) étaient stockés en base. Cause racine : `close_finished_matches` s exécutait APRÈS le stockage dans `run_collection`. Correctif : (1) clôture en tête de traitement + garde tipoff avant stockage dans le collecteur, (2) garde `tipoff > now` en tête d`analyze_match` (alertes + re-décision), (3) CLV déjà protégé (`snapshot_at <= tipoff`). Les 3 alertes Portland live (R1/R2/R4) n sont pas parties sur Telegram — Portland était DECIDE, donc non réanalysée. **Mais c était par coïncidence, pas par conception** : un match resté SUIVI au tip-off aurait alerté en live. La garde (2) rend la protection délibérée. Snapshots pollués non supprimés (append-only), neutralisés par les filtres. | Bug révélé par la simulation — sans elle, les cotes live auraient continué à entrer en base et potentiellement faussé de futures clôtures si le filtre CLV n existait pas. |
| 2026-07-17 | **Revue externe — 4 correctifs (C1/M1/M2/M3)** :
**(C1, critique) — `analyze_open_matches` sélectionnait uniquement DECOUVERT/SUIVI** : les matchs DECIDE n'étaient jamais réanalysés, donc la re-décision H-1, la supersession et l'annulation étaient du code mort en production (la branche `elif status == "DECIDE"` de `analyze_match` était inatteignable ; les tests passaient car `test_redecision.py` appelle `_redecide` directement). Correctif : la sélection utilise désormais `db.ACTIVE_STATUSES` (DECOUVERT/SUIVI/DECIDE) avec placeholders paramétrés. Test d'intégration manquant ajouté (`test_decide_match_in_window_is_redecided_and_superseded`) : un match DECIDE en fenêtre passé par `analyze_open_matches` est re-décidé, un changement matériel (sélection Home→Away) déclenche la supersession, et `logic_version` est estampillé. Vérification post-déploiement : mini-simulation sur base jetable via l'image Docker — C1 vérifié en production. **Effet sur les alertes** : les matchs DECIDE génèrent à nouveau des alertes R1/R2/R4 (la garde `_tipoff_passed` et la dédup `state_key` protègent contre le spam/post-tipoff). **Validation 7 jours repart à J0** (C1 change un comportement) avec un nouveau critère : « au moins une re-décision observée dans les logs » — l'absence de ce critère avait laissé C1 invisible pendant deux validations.
**(M1) — ENTRYPOINT `uv run --no-sync`** : l'ENTRYPOINT re-synchronisait les dépendances (dont le groupe dev) à chaque démarrage — téléchargement réseau + délai à chaque cron (visible dans les logs : `Downloading ruff / pygments` + `Installed 7 packages` à chaque collecte). Correctif : `ENTRYPOINT ["uv", "run", "--no-sync"]` + `--no-install-project` au build (le package local n'était pas installé au build car `uv sync` s'exécutait avant `COPY src/` — deux étapes de sync : deps seules d'abord, puis package local après le COPY). Résout l'entrée « resync deps au run » du journal.
**(M2) — `DECISION_LOGIC_VERSION` déplacée vers `common/db.py`** : le littéral `logic_version=2` d'`evaluator.py` remplacé par `DECISION_LOGIC_VERSION`. Décision : déplacement vers `common/db` (pas import direct depuis `analyzer/verdict`) car c'est une **constante de données** (lue par l'évaluateur pour la ségrégation des cohortes, écrite par l'analyseur pour l'estampillage) — elle appartient à la couche données au même titre que `ACTIVE_STATUSES`. Re-export depuis `analyzer/verdict` (`from common.db import DECISION_LOGIC_VERSION`) pour préserver les imports existants. Le garde-fou règle 11 se mesure sur la cohorte de calibration = version de logique courante : si la logique bump à v3, la cohorte suivra automatiquement.
**(M3) — `config.yaml` schedule + README** : la section `schedule` ne mentionnait pas le split H-1 en 2 blocs ni que `setup_cron.sh` est la source de vérité. Corrigé : documentation des 6 créneaux, mention explicite que le planning d'exécution vit dans `scripts/setup_cron.sh`, ajout de `h1_split_blocks: 2`. README : « 5 collectes/jour » → « 6 collectes/jour ». | C1 : la re-décision H-1 (livrée à l'étape 1.6) ne s'exécutait jamais en production — bug critique invisible car les tests appelaient `_redecide` directement. M1 : dépendance réseau + délai à chaque cron. M2 : littéral magique au lieu d'une constante. M3 : documentation divergente du cron réel. |
| 2026-07-17 | **Revue de logique externe — 3 entrées post-correctifs** :
**(1, prioritaire post-validation) — R4 sans plancher d'ampleur** : un book compte dans la synchro dès 0,1 pt de mouvement (`_direction` utilise `_EPS=1e-9`), d'où un déclenchement quasi permanent (cf. alertes de la semaine) qui rend ses 3 points non discriminants. Correctif prévu **après le jour 7** : plancher configurable par book (`min_move_prob` ~1 pt, `min_move_line` ~0,5 pt) dans `config.yaml` sous `R4_multi_bookmaker_sync`, à calibrer avec le taux de déclenchement mesuré pendant la validation. Script d'analyse créé (`scripts/analyze_r4_distribution.py`, lecture seule) : au jour 7, il affichera la distribution des ampleurs par book sur les déclenchements R4 réels — le plancher sera choisi sur cette distribution, pas sur l'intuition. **Trois effets attendus du plancher** : (a) chute du volume d'alertes R4 (voulue — le bruit d'équilibrage disparaît) ; (b) baisse mécanique des scores de signal donc seuil de 6 à réexaminer avec les données (règle 11 — pas avant 50–100 évaluations) ; (c) bump de `DECISION_LOGIC_VERSION` à 3 à l'application — les cohortes pré/post-plancher ne se comparent pas, le garde-fou règle 11 suivra automatiquement la nouvelle cohorte. Changement de comportement → nouveau J0 court ou à coupler avec la phase 2.
**(2, cosmétique) — R4 étiquette "fenêtre récente"** : le calcul R4 est `values[-1] - values[0]` (ouverture→dernier), identique à R1, mais l'étiquette disait "fenêtre récente". Corrigé → "depuis l'ouverture". Pas de changement de comportement, pas de nouveau J0.
**(3, documentation) — "ouverture" = première collecte de l'outil** : `series[0]` est le premier snapshot collecté par l'outil (09:00 Paris), pas l'ouverture réelle du marché. Des lignes ont pu bouger overnight avant que l'outil ne commence à suivre le match (lookahead antérieur possible). Précisé dans CLAUDE.md section 6.1 et README. Ce biais est inhérent au planning de collecte local et sera partiellement atténué en phase 3 (EC2 24/7, collecte plus précoce).
**Pistes V2 confirmées au journal** : (a) consensus ancré sharp books (Pinnacle/Circa, région `eu` — coût quota supplémentaire à arbitrer) ; (b) ordre d'origination des mouvements (qui bouge en premier ?) ; (c) RLM (Reverse Line Movement) inaccessible sans données de splits. | R4 sans plancher = 3 points non discriminants (déclenchement quasi permanent). Étiquette R4 fausse. "Ouverture" ambiguë. Pistes V2 pour enrichir le consensus et l'analyse d'origination. |
| 2026-07-18 | **Lot 2 : Architecture auto-ordonnancée (état non déployable)** — **FAIT** : tick 20 min, vagues 45 min, `collection_log` avec clé unique `(match_id, target_name)` (correction bug conception : déduplication par nom de cible au lieu de `hours_before` pour permettre plusieurs cibles avec même horaire), 6 cibles configurables (H-6, matin, H-3, verdict H-2, redecision H-1, closing H-0.25), priorités (1=haute, 2=basse), clôture per-match sur le marché du verdict de chaque match (correction bug conception : union des marchés de la vague → marché individuel), `window_hours=2.5` (H-3 hors fenêtre, H-2 dedans), `ConfigurationError` levée si `collector.targets` absent/vide (séparation responsabilités : collecteur lève, `__main__` attrape + log + notif + exit), garde `TEST_MODE` sur `--now` (refusé par défaut, autorisé uniquement si `TEST_MODE=true`), garde anti-gel collecte du matin (retourne AVANT vérification targets si hors fenêtre), 201 tests passed (0 failed, 0 skipped), ruff clean. **RESTE** : **bug critique snapshots post-tip-off** révélé par simulation (garde per-match défaillante, régression correctif 17/07 : la garde `tipoff > now` en tête d'`analyze_match` protège les alertes/re-décision mais pas le stockage dans `run_collection` — les matchs d'une vague étalée dont certains ont déjà commencé continuent à être collectés, les snapshots live entrent en base) ; simulation à refaire depuis un instant où les 6 matchs sont tous à venir (attendu : 6 collectes closing, une par match, chacune à H-0.25 de son tip-off, aucun snapshot après un coup d'envoi) ; `setup_cron.sh` 6 lignes → 1 battement `*/20` ; README ; journal ; déploiement selon rituel. **Commit 95773b0 poussé, NON DÉPLOYABLE.** | Corrections bugs conception validées par tests ; simulation partielle a révélé une régression critique (garde post-tip-off) — signalée telle quelle, non corrigée par manque de temps/contexte. État documenté pour reprise Claude Code. |
| 2026-07-14 | Création du document (V1) | — |
