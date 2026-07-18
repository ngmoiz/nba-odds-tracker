# Lot 1 : Correctifs - Résumé des modifications

## Statut : EN COURS

### Correctif 1 : CLV None/"n/d" ✅ DÉJÀ OK

**Fichiers** : `src/evaluator/clv.py`, `src/evaluator/reporting.py`

**Constat** : Le code gère déjà correctement :
- `clv.py` ligne 75-76 : Retourne `None` si `closing` ou `opening` manquant
- `reporting.py` ligne 49-53 : Affiche "CLV n/d" si `clv is None`

**Action** : Aucune modification nécessaire

---

### Correctif 2 : Grading garde-fou

**Fichier** : `src/evaluator/evaluator.py`

**Problème** : API balldontlie renvoie `status="post"` avec scores `0-0` (données invalides)
- Exemple : New York @ Dallas (verdict_id=2)
- Grading accepte 0-0 → push erroné

**Solution** : Ajouter garde-fou après ligne 81
```python
if result is None or not result.is_final:
    # ... skip ...
    continue

# NOUVEAU : Garde-fou scores plausibles
if result.home_score == 0 and result.away_score == 0:
    logger.warning(
        "Match %s : scores 0-0 invalides (API bug), skip évaluation.",
        match["match_id"]
    )
    summary["ungradable"] += 1
    continue
```

---

### Correctif 3 : Colonne invalidated

**Fichiers** : `src/common/db.py`, migration SQL

**Ajout colonne** :
```sql
ALTER TABLE evaluations ADD COLUMN invalidated BOOLEAN DEFAULT 0;
```

**Exclusion agrégations** : Modifier toutes les requêtes COUNT/SUM
```sql
-- Avant
SELECT COUNT(*) FROM evaluations

-- Après
SELECT COUNT(*) FROM evaluations WHERE invalidated = 0
```

**Fichiers à modifier** :
- `src/evaluator/reporting.py` : `success_rate()`, `_summary_line()`
- `src/evaluator/weekly.py` : Toutes les agrégations
- `src/evaluator/evaluator.py` : Compteur total_evals

---

### Correctif 4 : Neutraliser verdict_id=2

**Action** : SQL direct
```sql
UPDATE evaluations SET invalidated = 1 WHERE verdict_id = 2;
```

**Vérification** : Exclus du compteur et taux

---

### Correctif 5 : Affichage NO_BET

**Fichier** : `src/evaluator/reporting.py`

**Problème** : NO_BET affiche comme SIGNAL

**Solution** : Modifier `_position_label()` ligne 56-62
```python
def _position_label(action: str | None, outcome: str, verdict: str) -> str:
    if action is None:
        # NO_BET : afficher "aurait gagné/perdu"
        if verdict == "NO_BET":
            aurait = {WON: "aurait gagné", LOST: "aurait perdu", PUSH: "aurait fait push"}[outcome]
            return f" — ℹ️ {aurait}"
        return ""
    # ... reste inchangé
```

**Taux** : Modifier `success_rate()` pour n'agréger que SIGNAL/ANOMALIE
```python
def success_rate(lines: list[EvalLine]) -> float | None:
    # Exclure NO_BET du calcul
    decisive = [
        line for line in lines 
        if line.verdict in ("SIGNAL", "ANOMALIE") and line.outcome in (WON, LOST)
    ]
    if not decisive:
        return None
    return sum(line.outcome == WON for line in decisive) / len(decisive)
```

---

### Correctif 6 : Idempotence bilan

**Fichier** : `src/evaluator/evaluator.py`

**Problème** : Deux exécutions → deux bilans envoyés

**Solution** : Vérifier si bilan déjà envoyé aujourd'hui
```python
# Avant send_direct()
report_key = f"daily_report_sent_{today_cal}"
if db.get_meta(conn, report_key) == "true":
    logger.info("Bilan quotidien déjà envoyé pour %s, skip.", today_cal)
    return summary

# Après send_direct() réussi
if sent:
    db.set_meta(conn, report_key, "true")
```

---

## Tests requis

1. **CLV None** : Vérifier affichage "n/d" quand snapshot manquant
2. **Grading** : Vérifier rejet scores 0-0
3. **Invalidated** : Vérifier exclusion agrégations
4. **Verdict_id=2** : Vérifier neutralisé
5. **NO_BET** : Vérifier affichage "aurait gagné/perdu"
6. **Idempotence** : Vérifier pas de doublon bilan

---

## Implémentation

**Statut** : Spécification terminée, implémentation en attente validation
