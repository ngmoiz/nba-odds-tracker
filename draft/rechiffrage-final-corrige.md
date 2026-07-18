# Rechiffrage final corrigé (≤470 crédits/mois)

## Corrections des conséquences silencieuses

### (a) Totals : 1 seul relevé → R3 impossible

**Problème** : Plan initial collecte totals uniquement à H-2
- **1 seul relevé** par match
- R3 exige **3 relevés consécutifs** → impossible
- R4 compare depuis ouverture → impossible
- Formatage "ligne avant → après" → impossible
- **Conséquence** : Plus aucune alerte totals + code mort

**Usage actuel des totals** :
- R2 (steam move) : Détecte mouvement rapide sur totals
- Formatage : "ligne 163,5 → 162,5"
- **Aucun verdict totals** en base (0/6)

**Deux branches** :

#### Branche 1 : Abandon des totals
- **Retrait** : Totals de la config collecte + analyse
- **Coût** : 0 crédit (déjà non collectés sauf H-2)
- **Impact** : Perte alertes totals (jamais utilisées en pratique)
- **Avantage** : Pas de code mort

#### Branche 2 : Conserver totals sur 2+ cibles
- **Ajouter totals** : Matin + H-6 (minimum pour R3)
- **Coût** : +2 crédits/jour = +60 crédits/mois
- **Impact** : Alertes totals possibles
- **Inconvénient** : Dépasse budget (450 + 60 = 510)

**Décision** : **Branche 1 (abandon totals)**
- 0 verdict totals en 6 matchs
- Pas de demande utilisateur
- Économie vs complexité

---

### (b) Matin sans spreads → R1 perd ouverture

**Problème** : Matin à 1 marché (h2h seul)
- R1 mesure mouvement spread **depuis l'ouverture**
- Sans spreads au matin → ouverture spread = H-6
- **Perte** : Toute la trajectoire matin → H-6
- **Impact** : R1 a déclenché sur Minnesota et Atlanta

**Correction** : Matin à **h2h + spreads** (2 marchés)

---

## Rechiffrage option (ii) corrigée

### Architecture

**Cibles** :
1. **Matin** (quotidien) : **2 marchés** (h2h + spreads)
2. **H-6** : 2 marchés (h2h + spreads)
3. **H-3** : 2 marchés (h2h + spreads)
4. **H-2 (VERDICT)** : **2 marchés** (h2h + spreads, totals abandonnés)
5. **H-1 (RE-DÉCISION)** : 2 marchés (h2h + spreads)
6. **H-0.25 (CLÔTURE)** : Union marchés verdicts (1-2 marchés)

### Budget

**Par vague** :
- H-6 : 2 crédits
- H-3 : 2 crédits
- H-2 : 2 crédits (totals supprimés)
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 9,5 crédits/vague

**Par jour** :
- Matin : 2 crédits (h2h + spreads restaurés)
- Vagues : 1,33 × 9,5 = 12,6 crédits
- **Total** : 14,6 crédits/jour

**Par mois** : 14,6 × 30 = **438 crédits** ✅

**Marge** : 500 - 438 = **62 crédits** (12% réserve) ✅

---

## Garde de réserve : Ordre de priorité

### Code actuel (collector.py)

**Problème** : Bloque TOUTES les collectes conditionnelles sans distinction
```python
if not _check_reserve(conn, config, settings):
    return {"skipped": True, "reason": "reserve"}
```

**Conséquence** : En fin de mois, verdict/re-décision/clôture bloqués comme le matin

### Ordre de priorité requis

**Priorité 1 (JAMAIS bloquer)** :
- H-2 (verdict)
- H-1 (re-décision)
- H-0.25 (clôture)

**Priorité 2 (Bloquer en premier)** :
- Matin (découverte)
- H-6 (tracking)

**Priorité 3 (Bloquer ensuite)** :
- H-3 (tracking)

### Implémentation requise

**Config** : Ajouter `priority` aux cibles
```yaml
collector:
  targets:
    - hours_before: 6.0
      markets: [h2h, spreads]
      priority: 2  # Bloquer en premier
    - hours_before: 3.0
      markets: [h2h, spreads]
      priority: 3  # Bloquer ensuite
    - hours_before: 2.0
      markets: [h2h, spreads]
      purpose: verdict
      priority: 1  # JAMAIS bloquer
    - hours_before: 1.0
      markets: [h2h, spreads]
      purpose: redecision
      priority: 1  # JAMAIS bloquer
    - hours_before: 0.25
      markets: dynamic
      purpose: closing
      priority: 1  # JAMAIS bloquer
```

**Logique** :
```python
def _check_reserve(conn, config, target_priority):
    reserve = config["quota"]["reserve"]
    credits = get_credits_remaining(conn)
    
    if credits >= reserve:
        return True  # OK
    
    # Priorité 1 : JAMAIS bloquer
    if target_priority == 1:
        return True
    
    # Priorité 2-3 : Bloquer
    return False
```

---

## Résumé final

### Budget
- **438 crédits/mois** (vs 438 actuel)
- **Marge : 62 crédits** (12% réserve)
- **Cible ≤470** : ✅ Respectée

### Modifications
✅ **Totals abandonnés** : Retrait config + analyse (0 verdict, code mort évité)
✅ **Matin restauré** : h2h + spreads (R1 préservé)
✅ **Garde réserve** : Priorités verdict/re-décision/clôture > tracking > découverte

### Cibles finales
1. Matin : h2h + spreads (priorité 2)
2. H-6 : h2h + spreads (priorité 2)
3. H-3 : h2h + spreads (priorité 3)
4. H-2 (verdict) : h2h + spreads (priorité 1)
5. H-1 (re-décision) : h2h + spreads (priorité 1)
6. H-0.25 (clôture) : union marchés (priorité 1)

### Avantages
✅ **Budget identique** : 438 crédits (comme actuel)
✅ **CLV réel** : H-2 vs H-0.25
✅ **Re-décision** : Préservée
✅ **R1 préservé** : Spreads dès le matin
✅ **Densité R3** : 4 collectes/vague (h2h + spreads)
✅ **Garde intelligente** : Décisions prioritaires en fin de mois

### Sacrifices
⚠️ **Totals** : Abandonnés (0 verdict, jamais utilisés)
⚠️ **Marge** : 12% au lieu de 20% (acceptable)

---

## Implémentation

1. **Config** : Retirer totals, ajouter `priority` aux cibles
2. **Collecteur** : Logique priorité dans `_check_reserve()`
3. **Analyse** : Retirer totals des marchés analysés
4. **Tests** : Garde réserve respecte priorités
5. **Migration** : Aucune (pas de changement schéma)
