# Architecture : Collecteur auto-ordonnancé par vague (J0 - 18/07/2026)

## Principe

**Cron battement** (tick 20 min) → **Collecteur intelligent** qui décide quoi collecter

**Décision** : Palier payant 20K crédits/mois (30 $/mois)
- Budget : ~700 crédits/mois (3,5% du quota)
- Architecture complète conservée (totals partout, H-3, re-décision)

---

## 1. Cron battement

**Rôle** :
- Tick toutes les 20 min
- Aucun appel API par lui-même
- Lance le collecteur

**Cron** :
```
*/20 * * * * /app/collector
```

---

## 2. Collecteur auto-ordonnancé

### Algorithme (à chaque tick)

#### Étape 1 : Charger les matchs actifs
```python
from common import db

# JAMAIS de liste en dur (erreur C1)
active_matches = db.get_matches(conn, statuses=db.ACTIVE_STATUSES)
# ACTIVE_STATUSES = ['DECOUVERT', 'SUIVI', 'DECIDE']
```

#### Étape 2 : Grouper en vagues
- **Seuil** : 45 min (configurable)
- Matchs dont tip-offs ≤45 min d'écart → même vague

**Exemple** :
```
Match A: 01:00 UTC
Match B: 01:15 UTC  } Vague 1 (écart 15 min)
Match C: 01:30 UTC  }

Match D: 03:00 UTC  → Vague 2 (écart 90 min)
```

#### Étape 3 : Calculer cibles par vague

**Cibles** (6 au total) :
1. **Matin** (quotidien, 09:00) : 3 marchés (h2h + spreads + totals)
2. **H-6** : 3 marchés (h2h + spreads + totals)
3. **H-3** : 3 marchés (h2h + spreads + totals)
4. **H-2 (VERDICT)** : 3 marchés (h2h + spreads + totals)
5. **H-1 (RE-DÉCISION)** : 3 marchés (h2h + spreads + totals)
6. **H-0.25 (CLÔTURE)** : Union marchés verdicts (1-3 marchés)

**Config** :
```yaml
collector:
  tick_interval_minutes: 20
  wave_grouping_minutes: 45
  targets:
    - hours_before: 6.0
      markets: [h2h, spreads, totals]
      priority: 2
    - hours_before: 3.0
      markets: [h2h, spreads, totals]
      priority: 3
    - hours_before: 2.0
      markets: [h2h, spreads, totals]
      purpose: verdict
      priority: 1
    - hours_before: 1.0
      markets: [h2h, spreads, totals]
      purpose: redecision
      priority: 1
    - hours_before: 0.25
      markets: dynamic  # Union des marchés des verdicts
      purpose: closing
      priority: 1
```

#### Étape 4 : Vérifier cibles atteintes
- Cible atteinte : `NOW() >= (tipoff_median - hours_before)`
- Pas encore servie : Vérifier `collection_log`

#### Étape 5 : Collecter cibles dues
- Appel API avec marchés configurés
- Traçabilité dans `collection_log`

---

## 3. Traçabilité (table collection_log)

```sql
CREATE TABLE collection_log (
    id INTEGER PRIMARY KEY,
    wave_id TEXT NOT NULL,
    target_hours REAL NOT NULL,
    target_timestamp TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    markets TEXT NOT NULL,
    credits_used INTEGER NOT NULL,
    match_ids TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_wave_target ON collection_log(wave_id, target_hours);
```

**Anti-doublon** :
```sql
SELECT 1 FROM collection_log 
WHERE wave_id = ? AND target_hours = ?
```

---

## 4. Collecte de clôture (H-0.25)

### Union des marchés des verdicts

```sql
SELECT DISTINCT v.market 
FROM verdicts v
JOIN matches m ON v.match_id = m.match_id
WHERE m.match_id IN (wave_match_ids)
  AND m.status = 'DECIDE'
```

**Exemple** :
- Verdict 1 : spreads → Collecter spreads
- Verdict 2 : h2h → Collecter h2h
- **Union** : `markets=spreads,h2h`

### Pas de re-décision

**Rôle** : Capturer prix uniquement
- Pas d'analyse
- Pas de verdict
- Juste insertion `odds_snapshots`

### Garde anti-post-tip-off

```python
if NOW() >= tipoff_median:
    log("Vague déjà commencée, skip clôture")
    return
```

---

## 5. Garde de réserve par priorités

### Problème actuel

Code bloque TOUTES les collectes sans distinction

### Solution : Priorités

**Priorité 1** (JAMAIS bloquer) :
- H-2 (verdict)
- H-1 (re-décision)
- H-0.25 (clôture)

**Priorité 2** (bloquer en premier) :
- Matin
- H-6

**Priorité 3** (bloquer ensuite) :
- H-3

**Logique** :
```python
def _check_reserve(conn, config, target_priority):
    reserve = config["quota"]["reserve"]
    credits = get_credits_remaining(conn)
    
    if credits >= reserve:
        return True
    
    # Priorité 1 : JAMAIS bloquer
    if target_priority == 1:
        return True
    
    # Priorité 2-3 : Bloquer
    return False
```

---

## 6. Budget (calendrier WNBA juillet complet)

### Données réelles
- **72 matchs** sur 26 jours
- **56 vagues** (seuil 45 min)
- **Moyenne** : **2,15 vagues/jour**
- **Maximum** : 4 vagues/jour

### Budget cas moyen

**Par vague** :
- H-6 : 3 crédits
- H-3 : 3 crédits
- H-2 : 3 crédits
- H-1 : 3 crédits
- H-0.25 : 2 crédits (moyenne)
- **Total** : 14 crédits/vague

**Par jour** :
- Matin : 3 crédits
- Vagues : 2,15 × 14 = 30 crédits
- **Total** : 33 crédits/jour

**Par mois** : 33 × 30 = **~1000 crédits** (estimation haute)

**Palier payant** : 20 000 crédits/mois
- **Utilisation** : 5% du quota
- **Marge** : 19 000 crédits (95%)

---

## 7. Impact sur window_hours

**Actuel** : `window_hours: 2.0`
**Nouveau** : `window_hours: 3.0`

**Raison** :
- Verdict à H-2 (2h avant tip-off)
- Fenêtre 3.0 couvre H-3 → H-2
- Sécurité pour tip-offs décalés

---

## 8. Re-décision

**État** : **Implémentée et fonctionnelle**
- Code : `listener/handling.py` (protection anti-clic périmé)
- Démontrée en live le 16/07
- Ressuscitée par correctif C1

**Avec H-1** :
- Collecte 1h après verdict
- Permet re-décision basée sur mouvement H-2 → H-1
- Fenêtre 2h pour annuler

---

## 9. Implémentation

### Étapes

1. **Table `collection_log`** : Créer + index unique
2. **Config** : Ajouter `tick_interval_minutes`, `targets`, `wave_grouping_minutes`, `quota.monthly_credits: 20000`, `quota.reserve: 1000`
3. **Collecteur** :
   - Charger matchs actifs (ACTIVE_STATUSES)
   - Grouper en vagues (45 min)
   - Calculer cibles par vague
   - Vérifier cibles atteintes et non servies
   - Collecter + tracer
4. **Clôture** : Union marchés verdicts
5. **Garde réserve** : Logique priorités
6. **Cron** : Tick toutes les 20 min
7. **window_hours** : 3.0
8. **Tests** : Vérifier sur 6 matchs en base

### Tests critiques

- Vague avec 1 match : 6 collectes (matin + 5 cibles)
- Vague avec 3 matchs : 6 collectes (pas 18)
- Clôture : Union correcte des marchés
- Anti-doublon : Même cible pas servie 2 fois
- Garde post-tip-off : Clôture skippée si match commencé
- Garde réserve : Priorités respectées

---

## 10. Instrumentation

### Logging (chaque tick)

```python
logger.info(
    "Tick collecteur : %d vagues détectées, %d cibles dues, %d crédits consommés",
    nb_vagues, nb_cibles, credits_used
)
```

### Bilan quotidien

Ajouter ligne :
```
Crédits : 45 consommés / 19 955 restants, 3 vagues
```

---

## Résumé

**Architecture finale** :
- Cron : Tick 20 min
- Collecteur : Auto-ordonnancé par vague
- Cibles : Matin + H-6 + H-3 + H-2 (verdict) + H-1 (re-décision) + H-0.25 (clôture)
- Marchés : h2h + spreads + totals (partout)
- Budget : ~1000 crédits/mois (5% du quota 20K)
- Garde réserve : Priorités (verdict/re-décision/clôture jamais bloqués)
- Traçabilité : `collection_log` (anti-doublon)
- Clôture : Marchés dynamiques (union verdicts)
