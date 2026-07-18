# Analyse du projet & plan d'amélioration — NBA Odds Tracker

> Document de travail rédigé après lecture intégrale du code (`rules.py`, `verdict.py`,
> `scoring.py`, `clv.py`, `config.yaml`, `analyze_r4_distribution.py`) et des trois avis
> IA de `draft/note.md` (Grok, DeepSeek, Gemini).
>
> Objectif : séparer ce qui est **vrai et faisable à ton échelle** (dev solo, WSL2 local,
> API gratuite 500 crédits/mois, WNBA en cours) de ce qui est surinterprété ou hors de
> portée, puis proposer un plan d'amélioration concret et phasé.

---

## 1. Ce que fait vraiment le projet (vérifié dans le code)

Ton système est un **détecteur de flux d'argent / de mouvement de marché** (line-following),
proprement construit. Ce n'est **pas** un système de prédiction du résultat des matchs.

### 1.1 Ce qui est confirmé côté code

| Constat | Preuve dans le code |
|---|---|
| **CLV correctement calculé** | `clv.py` : `closing.prob − verdict.prob` sur la **proba dé-margée** du consensus médian. La boussole est juste. |
| **Aucune opinion indépendante sur l'issue** | Nulle part `rules.py`/`verdict.py` ne modélise la force des équipes. Le système ne compare que des cotes entre elles. |
| **R4 sans plancher d'ampleur** | `evaluate_r4` s'appuie sur `_direction`, qui utilise `_EPS=1e-9` → un book qui bouge de 0,1 pt compte comme « synchronisé ». Les +3 ne sont pas discriminants. |
| **R1 flat (non pondérée par l'ampleur)** | `evaluate_r1` : `score if triggered else 0`. Un mouvement de 2,0 pt et de 6,0 pt donnent le même +3. |
| **« Ouverture » = 1re collecte de l'outil (09:00 Paris)** | `series[0]` dans les règles ; biais documenté dans CLAUDE.md §6.1. |
| **Totals quasi aveugles au mouvement de ligne** | R1 ne couvre que `spreads` ; R3/R4 traitent les totals via la proba (plate), pas via le mouvement de la ligne totale. |

### 1.2 Ce qui est déjà bien fait (à conserver absolument)

- `NO_BET` par défaut, `SIGNAL` exigeant (score ≥ 6 + cohérence).
- **Re-décision H-1** avec gel après prise de position, supersession/annulation des messages.
- **Cohortes `logic_version`** pour ne pas polluer la calibration.
- **CLV** placé au centre (bonne métrique de long terme, meilleure que le win rate).
- **Garde-fou 50-100 évals** avant tout tweak de seuil (règle 11).
- Append-only sur `odds_snapshots`, dédup d'alertes par `state_key`, garde anti-post-tip-off.
- Règles explicables, seuils en YAML, tests déterministes sans quota.

> **En clair** : l'ingénierie est solide. Tu es déjà au-dessus de 90 % des projets amateurs.

---

## 2. Diagnostic honnête : machine à perdre ou système viable ?

**Ni l'un ni l'autre de façon binaire.**

- **Misé en réel, tel quel** : espérance **légèrement négative** (le vig ~4-5 % + les moves
  déjà digérés à H-1). Ce n'est pas un crash, c'est une **érosion lente**.
- **En paper trading discipliné, CLV comme boussole** : **excellent banc d'essai**. Tu apprends
  le marché et accumules des données propres. Rentable en *compétence*, pas encore en euros.

**La pièce manquante n'est pas une 8ᵉ règle de mouvement.** C'est une **opinion chiffrée sur le
résultat du match**, confrontée au prix. Sans elle, tu ne sais jamais si un mouvement **crée** de
la valeur (sharp money) ou la **détruit** (public money → *value trap*). Tu hérites de l'edge (ou
de l'erreur) de ceux qui font bouger la ligne.

### Nuance importante que les 3 IA sous-estiment

Les avis raisonnent implicitement **NBA** (marché le plus efficient du monde). **Or tu tournes en
WNBA** (`api.sport: basketball_wnba`) : liquidité plus faible, books plus mous, pricing plus lent
à s'équilibrer. Le line-following y a **objectivement plus de marge** qu'en NBA. Le verdict
« impossible de battre le marché » de Gemini est vrai pour la NBA, **surinterprété pour la WNBA**.
Cela ne change pas la conclusion (il faut un modèle de force), mais ça rend ta phase de test WNBA
plus informative qu'ils ne le laissent croire.

---

## 3. Tri des avis IA (Grok / DeepSeek / Gemini)

### ✅ Retenu — vrai ET faisable à ton échelle

| Recommandation | Pourquoi je la retiens | Effort |
|---|---|---|
| **Modèle de force indépendant (Elo/SRS + home + repos)** | Convergence des 3 IA. C'est LA transformation « suiveur → prédictif ». Données via balldontlie (déjà intégré, gratuit). | Moyen |
| **Plancher d'ampleur R4** | Confirmé dans le code, script déjà écrit (`analyze_r4_distribution.py`). Anti-bruit direct. | Faible |
| **Pondération progressive R1** | Un move de 5 pt ≫ un move de 2 pt. Trivial à coder, configurable. | Faible |
| **Filtre « move déjà refroidi »** | Si la ligne n'a plus bougé depuis N h → info déjà dans le prix → `NO_BET`. ~20 lignes dans `verdict.py`. | Faible |
| **Kill-switch chiffré dans `config.yaml`** | Évite le biais « je re-teste 7 jours indéfiniment ». Définir le critère d'échec maintenant. | Faible |
| **Discipline paper + CLV métrique unique** | Zéro code. Pure discipline. Déjà à moitié en place. | Nul |
| **Cooldown / anti-corrélation (1-2 expositions/soirée)** | 5 overs le même soir = 1 pari déguisé. Simple garde. | Faible |

### ⚠️ Partiellement retenu — vrai mais à différer / arbitrer

| Recommandation | Réserve | Décision |
|---|---|---|
| **Ancrage sharp (Pinnacle/Circa)** | Pinnacle est en région `eu` → **coût quota** sur 500 crédits/mois. | À arbitrer en Phase C, pas maintenant. Le schéma stocke déjà le `bookmaker`, rien n'est perdu. |
| **Key numbers NBA (3, 5, 7…)** (Gemini) | Pertinent en **NBA**, mais tu tournes en **WNBA** où ils sont moins marqués. | Reporté à la bascule NBA. Bonne idée, mauvais timing. |
| **Poisson bivarié / Monte-Carlo pour totals** | ROI inférieur à l'Elo, plus lourd. | Phase C, après le modèle de force. |
| **Kelly fractionnel** | N'a de sens **qu'après** CLV>0 prouvé sur 200+ évals. Kelly sur du bruit = ruine. | Phase C conditionnelle. Flat staking d'ici là. |

### ❌ Écarté — non véridique à ton échelle ou infaisable

| Recommandation | Raison de l'écarter |
|---|---|
| **RLM (Reverse Line Movement)** | Nécessite bet% / ticket% → **indisponible sur API gratuite**. Les 3 IA le reconnaissent. Abandonné. |
| **« Battre la NBA à H-2 est statistiquement impossible »** (Gemini) | Surinterprété, et hors sujet : tu es en WNBA (voir §2). Directionnellement utile, littéralement faux dans ton contexte. |
| **Intégration automatisée des injury reports temps réel** | Sourcing WNBA gratuit et fiable trop fragile pour de l'automatisé. Aspiration long terme, pas un chantier court. |
| **Détection HFT / « syndicats de sharps plus rapides »** (Gemini) | Vrai mais non actionnable : tu ne feras pas de la milliseconde avec du cron WSL2. À accepter comme limite structurelle, pas à « corriger ». |

---

## 4. Plan d'amélioration phasé

Légende effort : 🟢 faible · 🟡 moyen · 🔴 élevé. Impact : ★ à ★★★.

### Phase A — « Nettoyer le radar » (pendant / juste après les 7 jours — ne rien casser)

| # | Action | Effort | Impact | Go/No-go |
|---|---|---|---|---|
| A1 | **Discipline** : paper only, flat 1u notionnelle, CLV en métrique n°1, win rate en note de bas de page. | 🟢 (process) | ★★ | — |
| A2 | Lancer `analyze_r4_distribution.py` à **J7** → choisir le plancher R4 **sur données**. | 🟢 | ★★ | ≥ quelques alertes R4 en base |
| A3 | **Kill-switch** : **définir les valeurs maintenant** (au journal), ex. après **50 SIGNAL v3**, si CLV moyen glissant < 0 **et** win rate < 52,4 % → `NO_BET` forcé (mode observation). **Le code est livré avec la release v3, PAS pendant le gel.** | 🟢 (déf. maintenant / code v3) | ★★ | — |

> ⚠️ Ne modifier **aucun seuil** avant 50-100 évals (règle 11). La Phase A prépare, elle ne calibre pas.
> **Amendement (avis n°4)** : coder le kill-switch cette semaine violerait la discipline pour un mécanisme
> qui ne peut de toute façon se déclencher qu'**après 50 SIGNAL v3**. On fige donc ses valeurs au journal
> dès maintenant, mais on l'implémente dans la release v3.


### Phase B — « De suiveur à prédictif » (le cœur, post-J7)

| # | Action | Effort | Impact | Dépend de |
|---|---|---|---|---|
| B1 | **Plancher d'ampleur R4** (`min_move_line` / `min_move_prob`) + bump `logic_version → 3`. | 🟢 | ★★ | A2 (distribution) |
| B2 | **Pondération progressive R1** : +3 dès 2 pt, +1 à 4 pt, +1 à 6 pt (configurable). | 🟢 | ★ | — |
| B3 | **Filtre anti value-trap « move refroidi »** dans `verdict.py`, **mesuré en collectes consécutives (≥ 2-3), jamais en heures murales**, et **inapplicable aux mouvements détectés en fenêtre de décision H-1**. | 🟡 | ★★ | — |
| B4 | **Cooldown / anti-corrélation** : plafonner à 1-2 expositions corrélées par soirée. | 🟢 | ★ | — |
| B5 | **Modèle de force baseline (Elo/SRS + home + repos/B2B)** → `p_model`. Décision hybride : `SIGNAL` seulement si `sign(move)` cohérent **ET** `|p_model − p_mkt| > τ`. | 🟡 | ★★★ | balldontlie (déjà là) |

> **B5 est le chantier structurant.** Le mouvement *confirme*, le modèle *autorise*. Spec complète en §5.
>
> **Amendement B3 (avis n°4) — « refroidi » se compte en relevés, pas en heures.** Avec **6 collectes/jour
> à espacement irrégulier** (09:00, 15:00, 20:00, 23:00, 01:00, 02:45 — cf. `config.yaml`), « la ligne n'a
> plus bougé depuis N heures » est **illusoire** : une ligne peut avoir bougé à 23:30 sans que tu le voies
> entre deux relevés. La bonne définition est **« stable sur ≥ 2-3 collectes consécutives »** — cohérente
> avec `R3_sustained_trend.min_consecutive_snapshots: 3` déjà en place. **Ne pas** appliquer le filtre aux
> mouvements **frais** détectés dans la fenêtre H-1 (`decision.window_hours: 2.0`) : ce sont précisément les
> plus exploitables.


### Phase C — « Raffinement » (plus tard, données à l'appui)

| # | Action | Effort | Impact | Condition |
|---|---|---|---|---|
| C1 | Ancrage sharp (Pinnacle `eu`) — consensus pondéré. | 🟡 | ★★ | quota disponible |
| C2 | Totals : mouvement de ligne réel + Poisson/normale léger pour P(over). | 🔴 | ★★ | après B5 |
| C3 | Key numbers à la bascule **NBA** (pondération non linéaire de R1). | 🟡 | ★ | passage NBA |
| C4 | Kelly fractionnel (¼) sur edge shrinké. | 🟡 | ★★ | CLV>0 stable sur 200+ évals |

### Architecture cible (résumé visuel)

```
[Contexte]  repos / B2B / home / (blessures publiques plus tard)
      ↓
[Modèle de force]  Elo/SRS  →  p_model  (proba « vraie », indépendante du marché)
      ↓
[Marché]  proba dé-margée p_mkt  +  trajectoire (tes R1–R5)
      ↓
[Edge]    edge = p_model − p_mkt
      ↓
[Décision]  edge > τ  ET  sign(move) cohérent   → SIGNAL
            move fort mais edge ≈ 0 / négatif    → NO_BET (value trap évité)
      ↓
[Sizing]  flat 1u   (¼ Kelly seulement après CLV>0 prouvé)
```

Aujourd'hui tu n'as que la **ligne « Marché »**. B5 ajoute les lignes « Modèle » et « Edge ».

---

## 5. Spec technique complète — Modèle de force (B5)

Objectif : produire, pour chaque match, une **probabilité de victoire indépendante du marché**
(`p_model`) et une **fair line** (spread théorique), pour distinguer un mouvement qui *crée* de la
valeur d'un mouvement qui la *détruit*.

Choix de modèle : **Elo avec marge de victoire (MOV) + avantage du terrain + ajustement repos**.
C'est le meilleur rapport simplicité/robustesse pour un dev solo. Pas de ML, cohérent avec la
philosophie « règles explicables » du projet.

### 5.1 Nouvelle table de données

```sql
-- Ratings de force par équipe, mis à jour après chaque match évalué.
-- Alimentée par l'évaluateur (qui a déjà les scores finaux via balldontlie).
team_ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sport TEXT,               -- 'basketball_wnba' / 'basketball_nba' (ratings séparés par ligue)
  team TEXT,                -- nom normalisé (même normalisation que reconcile.py)
  rating REAL,              -- Elo courant (défaut 1500)
  games_played INTEGER,     -- nb de matchs intégrés (pour le K adaptatif / burn-in)
  last_game_date TEXT,      -- date du dernier match intégré (repos / B2B)
  updated_at TEXT,
  UNIQUE(sport, team)
)

-- Optionnel (traçabilité / debug) : historique des variations de rating.
rating_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sport TEXT, team TEXT,
  match_id TEXT,
  rating_before REAL, rating_after REAL,
  created_at TEXT
)
```

> **Source des données** : aucune nouvelle API. L'évaluateur récupère déjà les scores finaux via
> **balldontlie** (gratuit, NBA+WNBA). On branche la mise à jour des ratings **dans le même flux**
> que le calcul du CLV, au moment où le match passe `CLOS → EVALUE`.

### 5.2 Formules

**(a) Probabilité de victoire attendue (Elo standard)**

```
E_home = 1 / (1 + 10^(-(R_home + HFA - R_away) / 400))
```
- `R_home`, `R_away` : ratings des deux équipes.
- `HFA` : home field advantage exprimé **en points Elo**. Conversion : ~1 pt de spread ≈ 25 pts Elo
  en basket. Avantage terrain ≈ **+2,5 pt WNBA / +3 pt NBA** → `HFA ≈ 62` (WNBA), `~75` (NBA).
  Valeur **configurable** par ligue.

**(b) Ajustement repos / back-to-back (avant le calcul de E_home)**

```
R_home_adj = R_home + rest_adjustment(home)
R_away_adj = R_away + rest_adjustment(away)
```
avec `rest_adjustment` en points Elo (configurable) :
- B2B (match la veille) : **−50 Elo** (~ −2 pt de spread)
- 3 matchs en 4 nuits : **−25 Elo** supplémentaires
- ≥ 2 jours de repos : **0** (référence)

`last_game_date` (table `team_ratings`) fournit le nombre de jours de repos.

**(c) Mise à jour après résultat, avec marge de victoire (MOV multiplier, façon FiveThirtyEight)**

```
K_eff = K * mov_multiplier
mov_multiplier = ln(|marge| + 1) * (2.2 / (elo_diff_winner * 0.001 + 2.2))
R_home' = R_home + K_eff * (S_home - E_home)
R_away' = R_away - K_eff * (S_home - E_home)   # jeu à somme nulle
```
- `S_home` = 1 si l'équipe à domicile gagne, 0 sinon (push impossible sur le résultat sec).
- `elo_diff_winner` = (rating du gagnant + HFA le cas échéant) − rating du perdant.
- `K` : facteur d'apprentissage de base, **configurable** (défaut **20**). Option burn-in :
  `K = 40` tant que `games_played < 10` (convergence rapide en début de saison).
- Le `mov_multiplier` amortit l'effet des blowouts et corrige le biais d'auto-corrélation
  (une équipe forte qui écrase gagnerait trop de points sans lui).

**(d) Conversion rating → fair spread (pour comparer au marché des spreads)**

```
fair_spread_home = -(R_home_adj + HFA - R_away_adj) / 25
```
(25 pts Elo ≈ 1 pt de spread ; le signe négatif = un favori porte un spread négatif.)

**(e) Edge**

Deux marchés, deux façons de mesurer l'edge :
- **Moneyline (h2h)** : `edge = p_model − p_mkt`, où `p_mkt` = proba dé-margée du consensus
  (déjà calculée par `preprocessing.py` / réutilisée par `clv.py`).
- **Spread** : `edge_pts = fair_spread_home − market_spread_home` (en points de ligne).
  Convertible en proba via `Φ(edge_pts / σ)` avec σ ligue (~11,5 NBA, à calibrer WNBA) — réutilise
  l'idée déjà notée pour R7 V1.1.

### 5.3 Intégration dans le pipeline

**Lieu** : nouveau module `src/analyzer/model.py` (fonctions pures, testables sans base), + lecture
des ratings via `common/db.py`, + mise à jour des ratings dans `src/evaluator/`.

**Flux de mise à jour (write path)** — dans l'évaluateur, quand un match passe `EVALUE` :
```
1. Lire R_home, R_away depuis team_ratings (défaut 1500 si équipe inconnue).
2. Calculer E_home (avec HFA + rest).
3. Appliquer la MAJ MOV (formule c) → R_home', R_away'.
4. UPSERT team_ratings (rating, games_played+1, last_game_date, updated_at).
5. (option) INSERT rating_history.
```

**Flux de décision (read path)** — dans `verdict.py`, au moment de `decide()` :
```
1. p_model  = model.win_probability(data, config)     # via ratings courants
2. p_mkt    = proba dé-margée consensus de la sélection pressentie (déjà dispo)
3. edge     = p_model - p_mkt
4. Règle hybride :
     - si équipe inconnue / ratings immatures (games_played < N_min) → edge = None
       → on RETOMBE sur la logique actuelle (score ≥ 6) SANS blocage (transition douce)
     - sinon : SIGNAL exige  (score ≥ seuil)  ET  (sign(move) cohérent avec edge)  ET  (edge > τ)
     - move fort mais edge ≤ 0  → NO_BET  (value trap explicitement évité, tracé dans rationale)
```
- `τ` (seuil d'edge) et `N_min` (matchs minimum pour un rating « mûr ») : **configurables**.
- Le `rationale` Telegram gagne une ligne : `p_model = 57 % vs marché 53 % → edge +4 pts (modèle autorise)`.

> **Transition douce** : tant que les ratings ne sont pas mûrs (début de saison, équipe nouvelle),
> le modèle ne **bloque pas** — il laisse la logique actuelle décider. On évite ainsi de geler tous
> les signaux pendant le burn-in. L'edge devient bloquant seulement quand il est fiable.

### 5.4 Configuration ajoutée (`config.yaml`)

```yaml
model:
  enabled: true
  elo:
    initial_rating: 1500
    k_factor: 20
    k_factor_burnin: 40          # tant que games_played < burnin_games
    burnin_games: 10
    home_advantage_elo: 62       # ~2,5 pt WNBA ; ~75 pour la NBA
    elo_points_per_spread: 25    # 25 Elo ≈ 1 pt de spread
    rest_adjustment_elo:
      back_to_back: -50
      three_in_four: -25
      rested: 0
  decision:
    min_games_for_edge: 10       # ratings « mûrs » avant que l'edge devienne bloquant
    edge_threshold_prob: 0.02    # τ : edge minimal (2 pts de proba) pour autoriser un SIGNAL
  sigma_by_league:               # σ pour Φ(spread/σ) — spread ↔ proba
    basketball_nba: 11.5
    basketball_wnba: 10.5        # à calibrer sur données
```

### 5.5 Plan de tests (pytest)

- `test_model.py` (module pur, priorité) :
  - `E_home` symétrique (deux équipes égales sans HFA → 0,5) ;
  - HFA augmente bien `p_model` de l'équipe à domicile ;
  - MOV multiplier : un blowout fait moins gonfler le rating qu'attendu linéairement ;
  - somme nulle : `ΔR_home = −ΔR_away` ;
  - ajustement B2B : abaisse le rating effectif du bon côté ;
  - conversion fair_spread ↔ proba cohérente (round-trip via σ).
- `test_verdict.py` (extensions) :
  - edge > τ + move cohérent → `SIGNAL` ;
  - move fort mais edge ≤ 0 → `NO_BET` (value trap évité), rationale explicite ;
  - ratings immatures (`games_played < min`) → **retombée** sur la logique actuelle (pas de blocage).
- `test_evaluator.py` (extensions) :
  - MAJ des ratings idempotente sur un match déjà évalué (pas de double comptage) ;
  - équipe inconnue initialisée à `initial_rating`.

### 5.6 Impact sur les cohortes de calibration

L'activation du modèle **change le comportement de décision** → **bump `DECISION_LOGIC_VERSION`**.
Les cohortes pré/post-modèle ne se comparent pas ; le garde-fou règle 11 suivra automatiquement la
nouvelle cohorte (constante déjà centralisée dans `common/db.py`). **Séquencement précis des versions
en §5.9** (amendement n°4).


### 5.7 Séquence d'implémentation recommandée (B5)

```
1. model.py (fonctions pures Elo/MOV/HFA/rest) + tests  ← aucun effet de bord, se teste seul
2. Migration DB : tables team_ratings + rating_history (idempotente)
3. BACKFILL SAISON COMPLÈTE (amendé) : rejouer TOUTE la saison WNBA 2026 écoulée, pas seulement
   l'historique déjà en base (voir §5.8 ci-dessous). Ratings mûrs DÈS l'activation.
4. Read path : p_model + edge dans verdict.py, en mode NON bloquant d'abord (juste tracé/loggé)
5. Observer quelques jours l'edge en shadow (log sans influencer le verdict)
6. Activer la règle hybride bloquante + bump logic_version + nouveau J0
```

> Le mode **shadow** (étape 5) est clé : on mesure l'edge et le CLV qu'il aurait produit **avant**
> de laisser le modèle influencer les décisions. Zéro risque, données propres pour calibrer `τ`.

### 5.8 Backfill Elo — saison complète, pas seulement la base locale (amendement n°4)

**Problème corrigé.** Ma version initiale disait « backfill possible sur l'historique déjà en base ».
C'est **insuffisant** : la base ne contient qu'**~1 semaine** de matchs alors qu'on est à **mi-saison
WNBA**. Démarrer tous les ratings à `1500` aujourd'hui rendrait `p_model` **faux pendant des semaines**
et ne satisferait **jamais** `min_games_for_edge` → l'edge ne deviendrait jamais bloquant.

**Solution.** Un **backfill de la saison WNBA 2026 entière** via balldontlie :
1. `ResultsApiClient.get_games(start_date, end_date)` **supporte déjà** les plages de dates avec
   pagination par curseur (vérifié, `results_api_client.py` L86-107) → on récupère toute la saison en
   une passe.
2. **Rejeu chronologique** des matchs (ordre par `game_date`) dans le write path Elo (formule §5.2c),
   en partant de `initial_rating`.
3. **Respect des rate limits** du tier gratuit balldontlie : exécution **unique**, lente, avec **cache**
   local des réponses (on ne re-télécharge pas à chaque run). Un backfill de ~200 matchs se fait **une
   seule fois**.
4. Résultat : **ratings mûrs à l'activation**, `min_games_for_edge` satisfait immédiatement, edge fiable
   dès le premier jour de shadow.

> **⚠️ Pré-requis (non mentionné par l'avis, mais bloquant)** : `config.yaml` L111 indique que
> `results.games_path: /v1/games` est **le chemin NBA** (« WNBA = chemin dédié à confirmer »). **Avant**
> le backfill, il faut **confirmer l'endpoint WNBA** de balldontlie (chemin et éventuel paramètre de
> ligue), sinon le rejeu récupérerait des matchs NBA. À valider en tout début de B5.

> **Script dédié** : prévoir `scripts/backfill_elo.py` (hors pipeline cron), idempotent (relançable sans
> double comptage grâce au cache + à l'UPSERT keyé `(sport, team)`), avec log de progression.

### 5.9 Séquencement des versions de logique (amendement n°4)

Mieux vaut **deux resets de cohorte** propres que quatre. Regroupement retenu :

| Release | Contenu | `logic_version` | Cohorte / J0 |
|---|---|---|---|
| **v3** | B1 (plancher R4) + B2 (pondération R1) + B3 (move refroidi) + B4 (cooldown) + **le code du kill-switch A3** | **3** | **1er J0** — un seul reset pour tous les filtres de mouvement |
| **B5 shadow** | modèle de force en **lecture seule** (edge loggé, jamais bloquant) | **inchangé (3)** | **aucun bump** — le shadow ne modifie aucune décision, donc ne pollue aucune cohorte |
| **v4** | activation de la **règle hybride bloquante** (edge > τ requis) | **4** | **2e J0** — reset au moment où le modèle influence réellement les verdicts |

**Pourquoi ce découpage :**
- B1→B4 partagent la même nature (ils affinent le score de mouvement) → les grouper évite quatre
  micro-cohortes ingérables sur ton volume.
- **B5 en shadow ne bump PAS** : c'est le point clé. Tant que l'edge n'est que loggé, les verdicts v3
  restent v3 — on accumule des données de calibration de `τ` **sans casser** la cohorte en cours.
- Le bump v4 arrive **uniquement** quand la règle hybride devient bloquante : c'est là que le
  comportement de décision change vraiment.
- Le kill-switch A3 voyage **avec** la v3 (cf. §4 Phase A) : il n'a de sens qu'une fois 50 SIGNAL v3
  accumulés, donc le livrer dans la release v3 est le bon moment, pas pendant le gel.


---

## 6. Multi-ligue (WNBA ↔ NBA) — basculer en gardant des configs propres



**Contexte** : la NBA ne démarre qu'en octobre. La WNBA sert de **terrain
d'entraînement** (données réelles, pipeline rodé) en attendant. Objectif : pouvoir
**basculer entre les deux ligues** en conservant des **seuils propres à chacune**
(la WNBA a moins de books, des lignes plus nerveuses, un avantage du terrain
différent — les calibrations ne sont pas transposables, cf. §3 et les 3 avis IA).

### 6.1 État actuel (vérifié) : basculement partiel


- ✅ `--sport basketball_wnba` surcharge `config["api"]["sport"]` (flag dans
  `collector/__main__.py`) ; `api.sport` sert de défaut.
- ❌ **Tous les seuils sont plats et partagés** : `rules.*`, `decision.*`, les seuils
  « négligeables », et (à venir) `model.*` (HFA, σ). Changer de sport ne change
  **que** l'endpoint API, pas la calibration — or c'est précisément ce qui doit
  différer entre ligues.

### 6.2 Répartition commun vs propre à la ligue


| Propre à chaque ligue | Commun (partagé) |
|---|---|
| `R4.min_bookmakers` (moins de books en WNBA → 3 plutôt que 4) | `api.region`, `api.markets`, `api.odds_format` |
| `R1.threshold_points` + seuils « négligeables » (lignes WNBA nerveuses) | `quota.*` (budget global) |
| `decision.signal_score_threshold` | `results.*` (provider balldontlie) |
| `model.elo.home_advantage_elo` (~62 WNBA / ~75 NBA) | `display.timezone` |
| `model.sigma` (~10,5 WNBA / ~11,5 NBA) | structure du planning (créneaux conceptuels) |
| `model.elo.rest_adjustment_elo` (fatigue plus marquée en NBA/B2B) | — |

### 6.3 Schéma cible : `defaults` + `leagues` (merge profond), rétro-compatible


```yaml
active_sport: basketball_wnba   # ligue par défaut (surchargée par --sport)

defaults:                       # base commune (= config.yaml actuel, à l'identique)
  api: { region: us, markets: [h2h, spreads, totals], odds_format: decimal }
  quota: { monthly_credits: 500, reserve: 50, ... }
  rules:
    R1_spread_line_move: { threshold_points: 2.0, score: 3 }
    R4_multi_bookmaker_sync: { min_bookmakers: 4, score: 3 }
    # ... R2, R3, R5, R6, R7, seuils négligeables ...
  decision: { signal_score_threshold: 6, window_hours: 2.0 }
  results: { provider: balldontlie, ... }
  display: { timezone: Europe/Paris }
  # model: { ... }   # arrivera avec B5

leagues:                        # UNIQUEMENT les clés qui diffèrent (override par merge)
  basketball_wnba:
    api: { sport: basketball_wnba }
    rules: { R4_multi_bookmaker_sync: { min_bookmakers: 3 } }
    # model: { elo: { home_advantage_elo: 62 }, sigma: 10.5 }
  basketball_nba:
    api: { sport: basketball_nba }
    rules: { R4_multi_bookmaker_sync: { min_bookmakers: 4 } }
    # model: { elo: { home_advantage_elo: 75 }, sigma: 11.5 }
```

**Principe** : `leagues` ne contient que les **surcharges** ; tout le reste vient de
`defaults`. Un merge profond produit un dict résolu **de la même forme qu'aujourd'hui**.

### 6.4 Mécanisme de résolution (`common/config.py`)


```python
def deep_merge(base: dict, override: dict) -> dict:
    """Fusion récursive : override écrase base, dict par dict (pas de remplacement global)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out

def load_config(path=None, sport=None) -> dict:
    raw = yaml.safe_load(open(path or DEFAULT_CONFIG_PATH, encoding="utf-8"))
    sport = sport or raw.get("active_sport")
    return deep_merge(raw["defaults"], raw.get("leagues", {}).get(sport, {}))
```

- Le dict renvoyé garde la **forme plate actuelle** (`config["rules"][...]`,
  `config["api"]["sport"]`…) → **zéro changement** dans `rules.py`, `verdict.py`,
  l'évaluateur, etc.
- Seuls les **4 points d'entrée** (`collector`, `evaluator`, `notifier`, `listener`
  `__main__`) passent le sport : `config = load_config(sport=args.sport)`.

**Ordre de priorité du sport (inchangé dans l'esprit) :**
1. flag `--sport` (ponctuel, ex. tester la WNBA hors-saison NBA) ;
2. sinon `active_sport` de `config.yaml`.

### 6.5 Base de données : partagée (choix retenu = A)


**Décision : base SQLite partagée**, partitionnée par `sport`.
- Cohérent avec `matches.sport` (déjà présent) et le modèle Elo keyé `(sport, team)`
  (spec §5.1) : aucune collision possible entre ligues.
- **Point d'attention** : toute agrégation de stats (rapport hebdo, calibration,
  distribution R4) doit **filtrer par `sport`** pour ne pas mélanger WNBA et NBA.
  À auditer requête par requête au moment de l'implémentation.
- Repli connu si besoin d'isolation totale (**option B**, non retenue) :
  `DATABASE_PATH=data/wnba.db` vs `data/nba.db` via `.env` (déjà supporté) — deux
  crons, deux fichiers. À garder en tête si les cohortes `logic_version` WNBA/NBA
  devaient rester strictement étanches.

### 6.6 Impact sur le modèle de force (§5)


Déjà prévu multi-ligue : `team_ratings` et `rating_history` sont keyés `(sport, team)`,
et `home_advantage_elo` / `sigma` / `rest_adjustment_elo` vivent sous `leagues.*.model`.
Les ratings NBA démarreront à `initial_rating` (1500) en octobre, indépendamment de la
WNBA — pas de contamination inter-ligue.

### 6.7 Statut


- **À implémenter plus tard** (reporté à ta demande). Chantier **faible risque** et
  **hors gel des seuils** (règle 11) : on **réorganise** les valeurs existantes en
  `defaults`/`leagues`, on ne les **modifie** pas. Les nouveaux seuils propres à la NBA
  se calibreront quand la NBA aura tourné (≥ 50-100 évals NBA).
- Tests à prévoir (`test_config.py`) : merge profond correct, override partiel (seule
  `min_bookmakers` change), `--sport` prioritaire sur `active_sport`, sport inconnu →
  `defaults` seuls.

---

## 7. Réponse synthétique à la question de départ



- **Tel quel, misé en réel** : machine à perdre **lente** (le vig). Pas un désastre, une érosion.
- **Tel quel, en paper + discipline CLV + kill-switch** : **excellent banc d'essai**, rentable en
  compétence.
- **Potentiellement profitable** seulement avec : (1) le **modèle de force** (B5), (2) les **filtres
  anti value-trap** (R4 plancher, move refroidi), (3) un **sizing conservateur**, (4) assez
  d'échantillon pour prouver **CLV > 0** stable.

La frontière entre « observer le marché » et « extraire un edge », ce n'est pas une règle de
mouvement de plus — c'est une **opinion chiffrée sur qui gagne**, confrontée au prix. Le reste de
ton système (collecte, CLV, garde-fous, Telegram) est déjà la bonne fondation pour la porter.

### ⚠️ Statut permanent — l'écart papier / réel (amendement n°4)

À garder affiché en permanence, quel que soit l'avancement :

- Le **CLV que tu mesures est calculé sur la médiane US** que tu logges — **pas** sur la cote
  **Betclic réellement exécutable**. Il **surestime** donc l'edge encaissable en euros.
- B5 (le modèle de force) **ne corrige pas** cet écart : c'est **structurel** (source de prix ≠ book
  de mise). Un edge « papier » positif reste une hypothèse tant qu'il n'est pas confirmé sur des
  prix jouables.
- **Statut du projet jusqu'à preuve du contraire** : **laboratoire d'apprentissage du marché de
  premier ordre**, pas machine à cash. Le passage de « labo » à « edge réel » exige un critère de
  sortie explicite :

> **Critère de sortie du statut « labo » : CLV > 0 stable sur ≥ 100 évaluations en cohorte v4**
> (modèle hybride actif). En dessous, on reste en paper / observation, sans exception.

---


## 8. Prochaines étapes concrètes


1. **Maintenant → J7** : Phase A (discipline + kill-switch config). Ne toucher aucun seuil.
2. **J7** : lancer `analyze_r4_distribution.py`, décider le plancher R4 sur données.
3. **Post-J7** : Phase B dans l'ordre B1 → B2 → B3 → B4 → **B5** (spec §5, séquence §5.7).
4. **Plus tard** : Phase C selon données et quota.

> Rappel garde-fou : toute modification de seuil reste soumise à la règle des 50-100 évaluations
> (règle 11). La Phase A **prépare** sans calibrer.

---

## 9. Décision actée (après amendement n°4)

Les quatre amendements de l'avis n°4 sont **adoptés** (vérifiés contre le code). Synthèse
opérationnelle, à recopier en tête de journal :

1. **Backfill Elo = saison WNBA complète**, pas la base locale (§5.8). Pré-requis bloquant :
   confirmer l'endpoint WNBA de balldontlie avant tout rejeu.
2. **« Move refroidi » = collectes consécutives** (≥ 2-3), jamais des heures ; jamais appliqué en
   fenêtre H-1 (§4/B3).
3. **Séquencement** : v3 = B1+B4 (1 J0) → B5 shadow (sans bump) → v4 = hybride bloquant (2ᵉ J0)
   (§5.9).
4. **Kill-switch A3** : valeurs figées maintenant au journal, **code livré avec la v3**, pas pendant
   le gel (§4).
5. **Statut permanent** : labo tant que CLV > 0 n'est pas prouvé sur ≥ 100 évals v4 (§7).

> Ce document reste **documentation pure** : aucune ligne de code n'a été modifiée, rien ne
> s'exécute avant J7. Le premier acte concret sera `analyze_r4_distribution.py` à J7.

