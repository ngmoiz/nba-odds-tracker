# Diagnostic des anomalies du 18/07/2026

## Anomalie 1 : CLV systématiquement 0,0 🔴 CRITIQUE

### Cas analysé : Atlanta Dream @ Toronto Tempo

**Données du match** :
- Match ID : `571b28ddb7c28b45b2925d493d2085c8`
- Verdict ID : 3
- Verdict : SIGNAL, Atlanta Dream spreads -9.5 @ 1.91
- Décision : 2026-07-17T23:00:03 UTC (01:00:03 Paris)
- Tip-off : 2026-07-17T23:30:00 UTC (01:30 Paris)
- **Écart** : 30 minutes entre verdict et tip-off

**Snapshots collectés** :
- Total : 16 snapshots pour Atlanta Dream spreads
- Premier : 2026-07-16T11:04:00 UTC
- **Dernier** : 2026-07-17T23:00:03 UTC (moment du verdict)
- **Aucun snapshot entre 23:00 et 23:30** (pas de collecte H-1)

**Calcul CLV** :
```
Proba au verdict (23:00:03) :
  - snapshot_at: 2026-07-17T23:00:03.541762+00:00
  - odds (médiane): 1.9100
  - prob (dé-marginée): 0.500000

Proba de clôture (dernier avant 23:30) :
  - snapshot_at: 2026-07-17T23:00:03.541762+00:00  ← MÊME SNAPSHOT
  - odds (médiane): 1.9100
  - prob (dé-marginée): 0.500000

CLV = 0.500000 - 0.500000 = 0.000000
```

### 🚨 CAUSE PROUVÉE

Le code compare **le même snapshot à lui-même** car aucune collecte n'a eu lieu entre le verdict (23:00) et le tip-off (23:30).

**Pourquoi ?** Le planning de collecte H-1 n'a pas capturé ce match (verdict trop proche du tip-off, ou collecte manquée).

**Conséquence** : CLV = 0,0 est mathématiquement correct mais **inutile** (ne mesure rien).

---

## Anomalie 2 : Match gradé "push" avec score 0-0 🔴 CRITIQUE

### Cas analysé : New York Liberty @ Dallas Wings

**Données du match** :
- Match ID : `51f9e00bb8d7debd5a922a21f0736e36`
- Verdict ID : 2
- Verdict : NO_BET
- Tip-off : 2026-07-17T01:00:00 UTC (03:00 Paris)
- Évalué le : 2026-07-18T08:52:42 UTC

**Évaluation erronée** :
```sql
home_score: 0
away_score: 0
outcome: push  ← FAUX
```

**Vérification API balldontlie** :
```
New York Liberty @ Dallas Wings
  Date: 2026-07-17T01:00:00.000Z
  Status: post  ← Marqué comme terminé
  Score: 0 - 0  ← Scores invalides
```

### 🚨 CAUSE PROUVÉE

L'API balldontlie renvoie **status="post"** (terminé) mais **scores 0-0** (données invalides ou non disponibles).

Le code de grading accepte ces scores :
```python
# grading.py ligne 37
return _sign_to_outcome(home_score - away_score)
# 0 - 0 = 0 → PUSH
```

**Conséquence** : Évaluation fausse écrite dans une table append-only.

---

## Anomalie 3 : Deux bilans pour le 18/07 ⚠️

### Exécutions de l'évaluateur

**1ère exécution** : 2026-07-18T08:52:42 UTC
- 4 évaluations : verdicts 2, 4, 5, 6

**2ème exécution** : 2026-07-18T09:02:00 UTC
- 2 évaluations : verdicts 1, 3

**Total** : 6 évaluations uniques (pas de doublon grâce à la clé `verdict_id`)

### 🟡 CONSTAT

- ✅ **Pas de doublon** : chaque verdict évalué une seule fois
- ⚠️ **Deux bilans envoyés** : un à 08:52 (4 matchs) et un à 09:02 (6 matchs cumulés)
- ✅ **Comptage correct** : `COUNT(*) = 6`, `COUNT(DISTINCT verdict_id) = 6`

**Cause probable** : Deux exécutions manuelles ou cron + manuel.

**Impact** : Bruit dans les notifications, mais pas de corruption de données.

---

## Statuts API WNBA confirmés

**Tests réels effectués** :

1. **Match à venir** (19/07) : `status = "pre"`
2. **Match terminé** (15-18/07) : `status = "post"`

**Autres statuts possibles** (non observés) :
- Match en cours : probablement `"in_progress"` ou `"live"`
- Match reporté : probablement `"postponed"` ou `"cancelled"`

---

## Résumé des causes

| Anomalie | Cause prouvée | Impact |
|----------|---------------|--------|
| **CLV = 0,0** | Même snapshot comparé à lui-même (pas de collecte entre verdict et tip-off) | CLV inutile, ne mesure rien |
| **Push 0-0** | API renvoie status="post" avec scores invalides 0-0 | Évaluation fausse en base (append-only) |
| **Deux bilans** | Deux exécutions de l'évaluateur (08:52 et 09:02) | Bruit notifications, pas de corruption |

---

## Recommandations de correction

### Anomalie 1 - CLV
1. ✅ **Repli à None** : Si `opening` ou `closing` est None → CLV = None (pas 0)
2. ✅ **Logging explicite** : Logger quand CLV = None et pourquoi
3. ⚠️ **Garde-fou supplémentaire** : Si `opening.snapshot_at == closing.snapshot_at` → CLV = None (même point)

### Anomalie 2 - Grading
1. ✅ **Garde-fou scores** : Refuser grading si `home_score == 0 AND away_score == 0`
2. ✅ **Vérification status** : Ne grader que si `result.is_final == True` ET scores > 0
3. ✅ **Colonne invalidated** : Ajouter `invalidated BOOLEAN DEFAULT 0` dans `evaluations`
4. ✅ **Exclusion agrégations** : Exclure les évaluations invalidées du comptage et des taux

### Anomalie 3 - Bilans multiples
- ℹ️ **Acceptable** : Pas de corruption, juste du bruit
- 💡 **Amélioration future** : Détecter si un bilan a déjà été envoyé aujourd'hui (meta table)
