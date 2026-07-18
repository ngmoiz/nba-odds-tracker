# Décision : Passage au palier payant (18/07/2026)

## Contexte

**Contrainte budgétaire** : 500 crédits/mois (plan gratuit)
**Besoin réel** : ~700 crédits/mois (2,15 vagues/jour WNBA)

**Architecture CLV complète** :
- Verdict à H-2 (2h avant tip-off)
- Re-décision à H-1 (1h avant tip-off)
- Clôture à H-0.25 (15 min avant tip-off)
- Densité : 6 cibles (matin, H-6, H-3, H-2, H-1, H-0.25)
- Marchés : h2h + spreads + totals

---

## Décision

### Passage au palier payant

**Plan** : 20 000 crédits/mois pour 30 $/mois

**Budget** :
- Architecture complète : ~700 crédits/mois
- **Utilisation** : 3,5% du quota
- **Marge** : 19 300 crédits (96,5%)

### Arbitrages annulés

✅ **Totals conservés** partout (matin, H-6, H-3, H-2)
- Alertes totals utilisées toute la semaine
- Formatage "ligne avant → après" fonctionnel
- R2 (steam move) opérationnel

✅ **H-3 conservé**
- Densité R3 préservée (4 collectes/vague)
- Pas d'arbitrage de fonctionnalité pour raison budgétaire

✅ **Architecture complète**
- CLV réel (H-2 vs H-0.25)
- Re-décision (H-1)
- Densité optimale

---

## Garde de réserve par priorités

**Implémentation permanente** (pas un pis-aller)

**Priorités** :
1. **Priorité 1** (JAMAIS bloquer) : H-2 (verdict), H-1 (re-décision), H-0.25 (clôture)
2. **Priorité 2** (bloquer en premier) : Matin, H-6
3. **Priorité 3** (bloquer ensuite) : H-3

**Config** :
```yaml
quota:
  monthly_credits: 20000  # Palier payant
  reserve: 1000           # 5% du quota (protection permanente)
```

---

## Densité des collectes

### Décision : Pas de changement pour l'instant

**Actuel** : 6 collectes/jour (matin, H-6, H-3, H-2, H-1, H-0.25)

**Raison** :
- R2 et R3 calibrés sur 6 collectes/jour
- Seuils exprimés en relevés consécutifs et fenêtres horaires
- **Augmenter la fréquence modifierait le sens des règles**

**Chantier séparé** (après J7) :
- Bump `logic_version`
- Recalibration R2/R3
- Tests sur données historiques

---

## Implémentation

### (1) Lot de correctifs

✅ **CLV** : None/"n/d" jamais 0
✅ **Grading** : Garde-fou status post ET scores plausibles
✅ **Colonne invalidated** : Exclue agrégations + compteur 50
✅ **Neutralisation** : New York@Dallas (verdict_id=2)
✅ **Affichage NO_BET** : "aurait gagné/perdu", taux SIGNAL/ANOMALIE
✅ **Idempotence** : Bilan quotidien

### (2) Architecture auto-ordonnancée

✅ **Tick** : 20 min
✅ **Vagues** : 45 min
✅ **Table** : collection_log (traçabilité)
✅ **Cibles** : Matin/H-6/H-3/H-2/H-1/H-0.25 avec priorités
✅ **Clôture** : Union marchés verdicts
✅ **window_hours** : 3.0

### (3) Instrumentation

✅ **Logging** : Vagues détectées + crédits consommés (chaque tick)
✅ **Bilan** : Ligne "crédits : X consommés / Y restants, N vagues"

---

## Calendrier

**J0** : 18/07/2026 (aujourd'hui)
- Implémentation complète
- Tests sur 6 matchs en base
- Déploiement

**J7** : 25/07/2026
- Revue densité collectes
- Recalibration R2/R3 si nécessaire

---

## Coût mensuel

**30 $/mois** (palier payant)
- 20 000 crédits/mois
- ~700 crédits utilisés (3,5%)
- Marge confortable pour NBA octobre (~1000 crédits/mois)
