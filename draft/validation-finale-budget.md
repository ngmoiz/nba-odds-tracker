# Validation finale du budget (calendrier WNBA complet)

## (1) Recalcul sur calendrier WNBA juillet 2026

### Données réelles
- **Total matchs** : 72
- **Jours avec matchs** : 26
- **Total vagues** : 56 (seuil 45 min)
- **Moyenne** : **2,15 vagues/jour**
- **Maximum** : **4 vagues/jour**

**Erreur initiale** : 1,33 vagues/jour (calculé sur 3 jours seulement)

---

## Budget recalculé - Option actuelle (h2h + spreads)

### Cas moyen (2,15 vagues/jour)

**Par vague** :
- H-6 : 2 crédits
- H-3 : 2 crédits
- H-2 : 2 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 9,5 crédits/vague

**Par jour** :
- Matin : 2 crédits
- Vagues : 2,15 × 9,5 = 20,4 crédits
- **Total** : 22,4 crédits/jour

**Par mois** : 22,4 × 30 = **672 crédits** ❌

**Dépassement** : 672 - 500 = **172 crédits** (34% au-dessus)

### Pire cas (4 vagues/jour)

**Par jour** :
- Matin : 2 crédits
- Vagues : 4 × 9,5 = 38 crédits
- **Total** : 40 crédits/jour

**Par mois** : 40 × 30 = **1200 crédits** ❌

**Dépassement** : 1200 - 500 = **700 crédits** (140% au-dessus)

---

## (2) Totals : Correction de la justification

### Erreur factuelle

**Affirmation erronée** : "jamais utilisées en pratique"

**Réalité** :
- Alertes totals émises et lues toute la semaine
- Formatage "ligne 163,5 → 162,5" livré hier
- R2 (steam move) fonctionne sur totals

**Correction** : Les totals SONT utilisés, mais **0 verdict totals** (6 verdicts = 4 h2h + 2 spreads)

---

## Option alternative : Supprimer H-3 pour financer totals

### Justification

**H-3 (priorité 3)** :
- Encadré par H-6 et H-2 (1h d'écart avec H-2)
- Peu de mouvement capturé entre H-3 et H-2
- **Sacrifice acceptable** pour préserver totals

### Architecture

**Cibles** :
1. **Matin** : **3 marchés** (h2h + spreads + totals)
2. **H-6** : **3 marchés** (h2h + spreads + totals)
3. ~~H-3~~ : **supprimé**
4. **H-2 (VERDICT)** : 3 marchés (h2h + spreads + totals)
5. **H-1 (RE-DÉCISION)** : 2 marchés (h2h + spreads)
6. **H-0.25 (CLÔTURE)** : Union marchés verdicts (1-2 marchés)

### Budget

**Par vague** :
- H-6 : 3 crédits
- H-2 : 3 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 9,5 crédits/vague (identique)

**Par jour** :
- Matin : **3 crédits** (totals ajoutés)
- Vagues : 2,15 × 9,5 = 20,4 crédits
- **Total** : 23,4 crédits/jour

**Par mois** : 23,4 × 30 = **702 crédits** ❌

**Dépassement** : 702 - 500 = **202 crédits** (40% au-dessus)

**Comparaison** : 702 vs 672 = +30 crédits (pire que sans totals)

---

## Solution : Réduction drastique

### Option finale : H-6 + H-2 + H-1 + clôture (sans H-3)

**Cibles** :
1. **Matin** : 2 marchés (h2h + spreads)
2. **H-6** : 2 marchés (h2h + spreads)
3. ~~H-3~~ : supprimé
4. **H-2 (VERDICT)** : 2 marchés (h2h + spreads)
5. **H-1 (RE-DÉCISION)** : 2 marchés (h2h + spreads)
6. **H-0.25 (CLÔTURE)** : Union marchés (1-2 marchés)

### Budget cas moyen (2,15 vagues/jour)

**Par vague** :
- H-6 : 2 crédits
- H-2 : 2 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 7,5 crédits/vague

**Par jour** :
- Matin : 2 crédits
- Vagues : 2,15 × 7,5 = 16,1 crédits
- **Total** : 18,1 crédits/jour

**Par mois** : 18,1 × 30 = **543 crédits** ❌

**Dépassement** : 543 - 500 = **43 crédits** (9% au-dessus)

### Budget pire cas (4 vagues/jour)

**Par jour** :
- Matin : 2 crédits
- Vagues : 4 × 7,5 = 30 crédits
- **Total** : 32 crédits/jour

**Par mois** : 32 × 30 = **960 crédits** ❌

**Dépassement** : 960 - 500 = **460 crédits** (92% au-dessus)

---

## Mécanisme d'adaptation requis

### Problème

**Aucune configuration fixe ne tient dans 500 crédits** avec 2,15 vagues/jour

### Solution : Dégradation par priorité

**Seuils** :
- **< 2 vagues/jour** : Toutes cibles (H-6, H-3, H-2, H-1, H-0.25)
- **2-3 vagues/jour** : Supprimer H-3 (H-6, H-2, H-1, H-0.25)
- **> 3 vagues/jour** : Supprimer H-6 aussi (H-2, H-1, H-0.25 uniquement)

**Budget adaptatif** :
```python
if vagues_jour <= 2:
    cibles = [H6, H3, H2, H1, H0_25]  # 9,5 crédits/vague
elif vagues_jour <= 3:
    cibles = [H6, H2, H1, H0_25]      # 7,5 crédits/vague
else:
    cibles = [H2, H1, H0_25]          # 5,5 crédits/vague
```

**Budget mois (2,15 vagues/jour moyen)** :
- 60% jours à 2 vagues : 2 + (2 × 7,5) = 17 crédits/jour
- 40% jours à 3 vagues : 2 + (3 × 7,5) = 24,5 crédits/jour
- **Moyenne** : 20 crédits/jour × 30 = **600 crédits** ❌

**Toujours au-dessus de 500**

---

## Estimation NBA octobre

**NBA saison régulière** :
- ~15 matchs/jour (vs 2-3 WNBA)
- Regroupement plus dense (7pm, 7:30pm, 10pm ET)
- **Estimation** : 3-4 vagues/jour

**Budget** : Identique ou pire que WNBA

---

## Réserve de 50 crédits suffisante ?

### Priorité 1 (verdict, re-décision, clôture)

**Par vague** :
- H-2 : 2 crédits
- H-1 : 2 crédits
- H-0.25 : 1,5 crédit
- **Total** : 5,5 crédits/vague

**Avec 50 crédits** : 50 / 5,5 = **9 vagues**

**À 2,15 vagues/jour** : 9 / 2,15 = **4,2 jours** ✅

**À 4 vagues/jour (pire cas)** : 9 / 4 = **2,25 jours** ⚠️

**Conclusion** : 50 crédits suffisent pour 2-4 jours en fin de mois (acceptable)

---

## Recommandation finale

### Constat

**Impossible de tenir dans 500 crédits** avec :
- 2,15 vagues/jour (WNBA juillet)
- Architecture CLV (verdict H-2, clôture H-0.25)
- Re-décision (H-1)

### Options

**Option A** : Accepter dépassement temporaire
- Budget réel : 543 crédits/mois (cas moyen)
- Dépassement : 43 crédits (9%)
- **Risque** : Échec fin de mois

**Option B** : Réduire fenêtre de décision
- Verdict à H-1 (au lieu de H-2)
- Pas de re-décision
- Clôture à H-0.25
- **Budget** : 2 + (2,15 × 5,5) = 13,8 crédits/jour = **414 crédits/mois** ✅

**Option C** : Abandonner CLV
- Verdict à H-1 (pas de clôture dédiée)
- CLV = verdict vs dernier snapshot (comme avant)
- **Budget** : 2 + (2,15 × 4) = 10,6 crédits/jour = **318 crédits/mois** ✅

### Décision requise

Choisir entre :
1. **CLV réel** (543 crédits, dépassement 9%)
2. **CLV sans re-décision** (414 crédits, marge 17%)
3. **Pas de CLV** (318 crédits, marge 36%)
