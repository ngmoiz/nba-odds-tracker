# Arbitrage CLV - Version 2 (rechiffrée)

## Option B disqualifiée ❌

**Objection validée** : OVD = proba_verdict - proba_ouverture est **tautologique**

**Pourquoi ?**
- Les règles R1-R4 déclenchent **parce que** le marché a bougé depuis l'ouverture
- Tout SIGNAL aura un OVD positif **par construction**
- OVD mesure ce que les règles ont déjà mesuré
- **Ne dit rien sur la qualité de la décision**

**Le pouvoir prédictif du CLV** vient de ce qui se passe **après le verdict**, pas avant.

→ **Option B abandonnée**

---

## Option A rechiffrée avec 3 leviers

### Levier (a) : 1 collecte/vague (pas 2/jour)

**Distribution réelle des tip-offs** (données en base) :
```
16/07: 1 vague (23:10 UTC)
17/07: 2 vagues (01:00 et 23:30 UTC)
18/07: 1 vague (02:00 UTC)
```

**Moyenne** : **1,3 vague/jour** (pas 2)

### Levier (b) : Remplace 15:00 (pas ajout)

**Planning actuel** :
- 09:00 matin (découverte)
- **15:00 après-midi** ← LA MOINS INFORMATIVE (matchs à ~10h du tip-off)
- 20:00 H-6
- 23:00 H-3
- 01:00 H-1 bloc 1
- 02:45 H-1 bloc 2

**Nouveau planning** :
- 09:00 matin
- ~~15:00 après-midi~~ → **SUPPRIMÉE**
- 20:00 H-6
- 23:00 H-3 → **VERDICT** (au lieu de H-1)
- 01:00 H-0.25 **CLÔTURE** bloc 1 (remplace H-1 bloc 1)
- 02:45 H-0.25 **CLÔTURE** bloc 2 (remplace H-1 bloc 2)

**Bilan** : 6 collectes → 6 collectes (pas d'ajout)

### Levier (c) : Sous-ensemble de marchés

**API The Odds API** : Paramètre `markets` accepté ✅
- Exemple : `markets=h2h,spreads` ou `markets=spreads` uniquement

**Collecte clôture** :
- Actuelle (matin, H-6, H-3) : 3 marchés × 1 région = **3 crédits**
- Clôture : **1 marché** (celui du verdict) × 1 région = **1 crédit**

**Coût par collecte** :
- Matin, H-6, H-3 : 3 crédits
- Clôture : **1 crédit** (au lieu de 3)

---

## Rechiffrage complet

### Budget actuel (juillet)

**Collectes/jour** : 6
**Coût/collecte** : 3 crédits
**Total/jour** : 18 crédits
**Jours en juillet** : 24,3 jours (438 crédits utilisés)

### Budget nouveau (Option A avec 3 leviers)

**Collectes/jour** :
- 09:00 matin : 3 crédits
- ~~15:00 après-midi~~ : 0 (supprimée)
- 20:00 H-6 : 3 crédits
- 23:00 H-3 (verdict) : 3 crédits
- Clôture (1,3/jour) : 1,3 × **1 crédit** = 1,3 crédits

**Total/jour** : 3 + 3 + 3 + 1,3 = **10,3 crédits/jour**

**Économie** : 18 - 10,3 = **-7,7 crédits/jour** ✅

### Budget mensuel

**Nouveau** : 10,3 crédits/jour × 30 jours = **309 crédits/mois**

**Marge** : 500 - 309 = **191 crédits de réserve** ✅

**Conclusion** : **Option A tient largement dans les 500** 🎯

---

## Fenêtre de décision

### Actuel
- Verdict à H-1 (60 min avant tip-off)
- `window_hours: 2.0` → Verdict si tip-off dans les 2h

### Nouveau
- Verdict à H-2 (120 min avant tip-off)
- `window_hours: 2.0` → **H-2 est exactement à la limite** ⚠️

**Risque** : Si tip-off à 01:00 et collecte H-3 à 22:00, écart = 3h → **hors fenêtre**

**Solution** : Ajuster `window_hours: 3.0` (au lieu de 2.0)
- Permet verdict entre H-3 et H-2
- Garde de sécurité pour les tip-offs décalés

---

## Impact sur la re-décision

**Actuel** : Pas de re-décision implémentée (étape 1.4 en attente)

**Nouveau** : Verdict à H-2 laisse **2h** avant tip-off
- Si re-décision implémentée : fenêtre de 2h pour changer d'avis
- Collecte clôture à H-0.25 : dernière chance de voir un mouvement majeur

**Avantage** : Plus de temps pour analyser et potentiellement annuler un signal douteux

---

## Résumé Option A (rechiffrée)

### Architecture
1. **Verdict à H-2** (collecte H-3, décision 1h après)
2. **Collecte clôture à H-0.25** (15 min avant tip-off)
3. **Suppression collecte 15:00** (la moins informative)

### Coût
- **10,3 crédits/jour** (au lieu de 18)
- **309 crédits/mois** (au lieu de 540 estimé initialement)
- **Économie de 7,7 crédits/jour** ✅

### Budget
- Actuel : 438 crédits/24,3 jours
- Nouveau : **309 crédits/mois**
- **Marge : 191 crédits** (38% de réserve) ✅

### Ajustements nécessaires
1. `window_hours: 2.0` → `3.0` (sécurité)
2. Planning cron : déplacer H-1 → H-3 pour verdict
3. Collecte clôture : paramètre `markets` dynamique (marché du verdict)

### Avantages
✅ **CLV réel** : Mesure "beat the close"
✅ **Budget respecté** : 309/500 (62% utilisé)
✅ **Économie** : -7,7 crédits/jour vs actuel
✅ **Fenêtre décision** : 2h pour prendre position
✅ **Re-décision** : Temps pour annuler si besoin

### Inconvénients
⚠️ Verdict plus tôt : cotes peuvent bouger défavorablement entre H-2 et H-0.25
⚠️ Complexité : collecte clôture avec marché dynamique
⚠️ Ajustement `window_hours` nécessaire

---

## Recommandation finale : **Option A** 🎯

**Justification** :
1. **Budget** : Tient dans les 500 avec 38% de marge
2. **Métrique** : CLV réel, conforme à la littérature
3. **Économie** : Réduit les coûts de 43% vs actuel
4. **Qualité** : Permet vraie évaluation de la qualité des signaux

**Implémentation** :
1. Supprimer collecte 15:00
2. Déplacer verdict de H-1 à H-2 (collecte H-3)
3. Ajouter collecte clôture H-0.25 avec marché dynamique
4. Ajuster `window_hours: 3.0`
5. Tests sur données historiques avant déploiement
