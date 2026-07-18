# Arbitrage d'architecture : CLV = 0 par construction

## Problème structurel identifié

Le CLV est **systématiquement 0** non par accident, mais **par construction** :

**Architecture actuelle** :
- Verdict rendu à H-1 (dernière collecte avant tip-off)
- Clôture = dernier snapshot avant tip-off
- **Verdict ET clôture pointent vers le même snapshot**
- CLV = proba_clôture - proba_verdict = **0 par définition**

**Données réelles** (6 verdicts analysés) :
```
Verdict 1:   9 min avant tip-off
Verdict 3:  29 min avant tip-off (Atlanta)
Verdict 4:  29 min avant tip-off
Verdict 5:  39 min avant tip-off
Verdict 6:  74 min avant tip-off
Verdict 2: 119 min avant tip-off
```

**Planning de collecte actuel** :
- 6 créneaux/jour : matin, après-midi, H-6, H-3, H-1 bloc 1, H-1 bloc 2
- Coût : 3 crédits/collecte × 6 = **18 crédits/jour**
- Budget mensuel : 500 crédits → **27,7 jours** (438 crédits utilisés en juillet)

---

## Option A : Découpler décision et clôture

### Architecture proposée

1. **Verdict à H-2** (au lieu de H-1)
   - Décision 2h avant tip-off
   - Laisse du temps pour une collecte "clôture"

2. **Collecte clôture dédiée à H-0.25** (15 min avant tip-off)
   - Nouvelle collecte juste avant le match
   - Capture les mouvements de dernière minute

### Coût en crédits

**Collectes supplémentaires** :
- +1 collecte "clôture" par jour de match
- WNBA : ~6 matchs/jour en saison régulière
- Mais les matchs sont groupés (2-3 blocs horaires)
- **Estimation** : +2 collectes/jour (une par bloc horaire)

**Nouveau budget** :
- Actuel : 6 collectes/jour × 3 crédits = 18 crédits/jour
- Nouveau : 8 collectes/jour × 3 crédits = **24 crédits/jour**
- **+6 crédits/jour** = **+180 crédits/mois**

**Impact sur budget 500** :
- Actuel : 438 crédits utilisés (27,7 jours)
- Nouveau : 438 + 180 = **618 crédits/mois**
- **DÉPASSEMENT de 118 crédits** ❌

### Impact sur fenêtre de décision

**Actuel** : Verdict à H-1 (60 min avant tip-off)
- Temps pour prendre position : 60 min
- Cotes encore disponibles

**Nouveau** : Verdict à H-2 (120 min avant tip-off)
- Temps pour prendre position : 120 min
- **Risque** : Cotes peuvent bouger défavorablement entre H-2 et H-1
- **Avantage** : Plus de temps pour analyser

### Avantages
✅ CLV mesure réellement le "beat the close"
✅ Conforme à la littérature
✅ Métrique fiable pour évaluer la qualité des signaux

### Inconvénients
❌ **Dépassement budget** : +118 crédits/mois (23% au-dessus du plan gratuit)
❌ Verdict plus tôt : risque de cotes moins favorables
❌ Complexité : gérer deux fenêtres de décision

---

## Option B : Redéfinir la métrique "Delta ouverture → verdict"

### Architecture proposée

**Renommer et redéfinir** :
- Ancienne métrique : "CLV" (Closing Line Value)
- **Nouvelle métrique** : "OVD" (Opening-to-Verdict Delta)

**Calcul** :
```
OVD = proba_verdict - proba_ouverture
```

**Interprétation** :
- OVD > 0 : Le marché s'est déplacé **vers** notre sélection depuis l'ouverture
- OVD < 0 : Le marché s'est déplacé **contre** notre sélection
- OVD = 0 : Pas de mouvement (ou verdict à l'ouverture)

### Coût en crédits

**Aucun coût supplémentaire** ✅
- Utilise les snapshots déjà collectés
- Ouverture = premier snapshot du match (collecte "matin")
- Verdict = snapshot au moment de la décision

### Impact sur fenêtre de décision

**Aucun changement** ✅
- Verdict reste à H-1
- Fenêtre de décision inchangée

### Avantages
✅ **Budget respecté** : 0 crédit supplémentaire
✅ **Métrique utile** : Mesure si on a "surfé" le mouvement du marché
✅ **Simplicité** : Pas de changement d'architecture
✅ **Données déjà disponibles** : Ouverture collectée chaque matin

### Inconvénients
⚠️ **Pas un CLV au sens strict** : Ne mesure pas "beat the close"
⚠️ **Nom trompeur** : Faut renommer pour éviter confusion
⚠️ **Littérature** : Moins standard que le CLV classique

### Exemple concret (Atlanta)

**Données** :
- Ouverture (16/07 11:04) : odds 1.91 → prob ~0.500
- Verdict (17/07 23:00) : odds 1.91 → prob ~0.500
- **OVD = 0.500 - 0.500 = 0.000**

**Interprétation** : Pas de mouvement entre ouverture et verdict (ligne stable).

---

## Recommandation : **Option B** 🎯

### Justification

1. **Budget** : Option A dépasse le plan gratuit de 23% (118 crédits/mois)
   - Nécessiterait un upgrade vers plan payant
   - Coût récurrent non justifié pour une métrique

2. **Utilité** : OVD reste une métrique **pertinente**
   - Mesure si le signal capte un mouvement de marché
   - Corrélation attendue : bon signal → OVD positif
   - Permet d'évaluer la qualité du timing

3. **Pragmatisme** : Option B est **immédiatement applicable**
   - Pas de changement d'architecture
   - Pas de coût supplémentaire
   - Données déjà disponibles

4. **Évolution future** : Si budget augmente
   - On pourra passer à Option A plus tard
   - OVD reste utile même avec un vrai CLV

### Implémentation recommandée

1. **Renommer** :
   - `clv` → `ovd` (Opening-to-Verdict Delta)
   - Docstrings et commentaires mis à jour

2. **Calcul** :
   ```python
   ovd = verdict_prob - opening_prob
   ```

3. **Affichage** :
   - "OVD +3,0 pts" (au lieu de "CLV +3,0 pts")
   - Tooltip : "Mouvement du marché depuis l'ouverture"

4. **Garde-fou** :
   - Si ouverture ou verdict manquant → OVD = None
   - Affichage : "OVD n/d"

---

## Conclusion

**Option B (OVD)** est la solution optimale pour :
- ✅ Respecter le budget (438/500 crédits)
- ✅ Avoir une métrique utile et calculable
- ✅ Éviter la complexité d'une collecte supplémentaire
- ✅ Garder la fenêtre de décision à H-1

**Option A (vrai CLV)** reste envisageable si :
- Budget augmente (plan payant ou quota plus élevé)
- Besoin de conformité stricte avec la littérature
- Évaluation académique du système
