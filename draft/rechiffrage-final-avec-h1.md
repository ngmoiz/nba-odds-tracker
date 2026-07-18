# Rechiffrage final avec H-1 conservé (≤450 crédits/mois)

## Erreur corrigée : Re-décision EST implémentée

**Constat** : La re-décision est implémentée, testée, démontrée en live le 16/07
- Code : Protection anti-clic périmé (`stale`) dans `listener/handling.py`
- Mécanisme : Verdict remplacé → ancien message périmé → clic rejeté
- **Critique** : Sans collecte post-verdict (H-1), aucune re-décision possible
  - Forfait annoncé 40 min avant match → pas de correction
  - Mouvement défavorable → pas d'annulation

**Levier 4 refusé** : Supprimer H-1 débrancherait la re-décision une seconde fois

---

## Option (i) : Remplacer H-3 par H-1 (à 2 marchés)

### Justification
- **H-2 et H-1** rendent H-3 largement redondant
- H-3 : 3h avant tip-off (tracking)
- H-2 : 2h avant tip-off (verdict)
- H-1 : 1h avant tip-off (re-décision)
- **H-3 et H-2** : 1h d'écart seulement → peu de mouvement capturé

### Architecture
**Cibles** :
1. **Matin** (quotidien) : 2 marchés (h2h + spreads)
2. **H-6** : 2 marchés (h2h + spreads)
3. ~~H-3~~ : **supprimé**
4. **H-2 (VERDICT)** : 3 marchés (h2h + spreads + totals)
5. **H-1 (RE-DÉCISION)** : 2 marchés (h2h + spreads)
6. **H-0.25 (CLÔTURE)** : Union marchés verdicts (1-2 marchés)

### Budget
**Par vague** :
- H-6 : 2 crédits
- H-2 : 3 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 8,5 crédits/vague

**Par jour** :
- Matin : 2 crédits
- Vagues : 1,33 × 8,5 = 11,3 crédits
- **Total** : 13,3 crédits/jour

**Par mois** : 13,3 × 30 = **399 crédits** ✅

**Marge** : 500 - 399 = **101 crédits** (20% réserve)

---

## Option (ii) : Garder H-3 et H-1, matin réduit

### Justification
- **Préserver densité R3** : H-6, H-3, H-1, H-2 = 4 collectes/vague
- **Matin en découverte seule** : 1 marché (h2h uniquement)
  - Découverte des matchs (API renvoie tous les matchs)
  - Ouverture h2h suffit (spreads rarement disponibles tôt)

### Architecture
**Cibles** :
1. **Matin** (quotidien) : **1 marché** (h2h seul)
2. **H-6** : 2 marchés (h2h + spreads)
3. **H-3** : 2 marchés (h2h + spreads)
4. **H-2 (VERDICT)** : 3 marchés (h2h + spreads + totals)
5. **H-1 (RE-DÉCISION)** : 2 marchés (h2h + spreads)
6. **H-0.25 (CLÔTURE)** : Union marchés verdicts (1-2 marchés)

### Budget
**Par vague** :
- H-6 : 2 crédits
- H-3 : 2 crédits
- H-2 : 3 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 10,5 crédits/vague

**Par jour** :
- Matin : **1 crédit** (h2h seul)
- Vagues : 1,33 × 10,5 = 14 crédits
- **Total** : 15 crédits/jour

**Par mois** : 15 × 30 = **450 crédits** ✅

**Marge** : 500 - 450 = **50 crédits** (10% réserve)

---

## Comparaison

| Option | Cibles | Crédits/mois | Marge | Densité R3 | Re-décision |
|--------|--------|--------------|-------|------------|-------------|
| **(i)** | H-6, H-2, H-1, H-0.25 | 399 | 101 (20%) | ⚠️ Réduite (3) | ✅ Oui |
| **(ii)** | H-6, H-3, H-2, H-1, H-0.25 | 450 | 50 (10%) | ✅ Préservée (4) | ✅ Oui |

---

## CLV : Dernier verdict en vigueur (cas nominal)

### Règle
**CLV se mesure contre le dernier verdict en vigueur**
- Pas de re-décision → CLV = clôture (H-0.25) - verdict (H-2)
- Re-décision à H-1 → CLV = clôture (H-0.25) - verdict_rafraîchi (H-1)

### Implémentation
**Code actuel** (`clv.py` ligne 72) :
```python
opening = verdict_point(data, market, selection, decided_at)
```

`decided_at` est le timestamp du verdict en base
- Si re-décision → nouveau verdict avec nouveau `decided_at` (H-1)
- `verdict_point()` prend le dernier snapshot ≤ `decided_at`
- **Fonctionne correctement** ✅

### Test requis
```python
def test_clv_uses_latest_verdict_after_redecision():
    """CLV se calcule contre le dernier verdict (H-1 si re-décision)."""
    # Verdict initial H-2 : odds 1.91
    # Re-décision H-1 : odds 1.85 (mouvement défavorable)
    # Clôture H-0.25 : odds 1.80
    # CLV = prob(1.80) - prob(1.85) (pas prob(1.91))
```

---

## Recommandation : **Option (ii)** 🎯

### Justification

1. **Re-décision préservée** : H-1 conservé
   - Forfait 40 min avant → correction possible
   - Mouvement défavorable → annulation possible

2. **Densité R3 préservée** : 4 collectes/vague
   - H-6, H-3, H-1, H-2
   - R3 (mouvement de ligne) garde sa puissance

3. **Budget respecté** : 450 crédits/mois
   - Marge 50 crédits (10% réserve)
   - Acceptable pour fin de mois

4. **Matin optimisé** : 1 marché (h2h)
   - Découverte suffit (API renvoie tous les matchs)
   - Spreads rarement disponibles tôt le matin

### Sacrifices acceptables
⚠️ **Matin** : 1 marché au lieu de 2 (spreads manquants)
⚠️ **Marge réduite** : 50 crédits (10% au lieu de 20%)

### Avantages
✅ **Re-décision** : Fonctionnelle
✅ **CLV réel** : H-2 vs H-0.25 (ou H-1 vs H-0.25 si re-décision)
✅ **Densité R3** : 4 collectes/vague
✅ **Budget** : 450/500 (90% utilisé)

---

## Implémentation

1. **Table `collection_log`** : Traçabilité anti-doublon
2. **Config** : Cibles avec marchés différenciés
   ```yaml
   collector:
     targets:
       - hours_before: 6.0
         markets: [h2h, spreads]
       - hours_before: 3.0
         markets: [h2h, spreads]
       - hours_before: 2.0
         markets: [h2h, spreads, totals]
         purpose: verdict
       - hours_before: 1.0
         markets: [h2h, spreads]
         purpose: redecision
       - hours_before: 0.25
         markets: dynamic
         purpose: closing
   ```
3. **Matin** : 1 marché (h2h)
4. **Collecteur** : Vagues + cibles + union marchés clôture
5. **window_hours** : 3.0
6. **Tests** : CLV dernier verdict, re-décision, verdicts précoces
