# État Lot 2 — Point d'entrée Claude Code

> **Contexte** : Migration Cline → Claude Code. Ce document est le point d'entrée de la première session. Lire CLAUDE.md intégralement avant toute action.

---

## Dernier commit

**Commit** : `95773b0` (2026-07-18)  
**Message** : Lot 2: corrections bugs conception (closing per-match + déduplication target_name)  
**État** : ⚠️ **NON DÉPLOYABLE** (bug critique snapshots post-tip-off)

**Tests** : 201 passed, 0 failed, 0 skipped  
**Lint** : ruff clean

---

## Trois décisions verrouillées du Lot 2

1. **Déduplication par `(match_id, target_name)`** — Permet plusieurs cibles avec même `hours_before` (ex: verdict H-2 + H-2-bis sur marchés différents). Clé unique modifiée dans `collection_log`.

2. **Closing per-match sur le marché du verdict** — Chaque match collecté à H-0.25 sur son propre marché de verdict (h2h ou spreads), pas sur l'union des marchés de la vague. Boucle per-match dans `collector.py`.

3. **`window_hours = 2.5`** — Verdict à H-2 (H-3 hors fenêtre, H-2 dedans), re-décision à H-1. Fenêtre de décision = 2,5 heures avant tip-off.

---

## Bug ouvert (critique)

**Symptôme** : Snapshots post-tip-off stockés en base (cotes live).

**Cause probable** : La garde `tipoff > now` en tête d'`analyze_match` protège les alertes/re-décision mais **pas le stockage** dans `run_collection`. Les matchs d'une vague étalée dont certains ont déjà commencé continuent à être collectés → snapshots live entrent en base.

**Régression** : Correctif 17/07 (bug snapshots post-tip-off) avait ajouté la garde dans `analyze_match`, mais le collecteur n'a pas de garde équivalente avant `store_snapshots`.

**Attendu** : Aucun snapshot après un coup d'envoi (garde per-match dans le collecteur).

---

## Séquence restante (dans l'ordre)

1. **Corriger bug snapshots post-tip-off** — Ajouter garde `tipoff > now` dans `run_collection` avant stockage, ou filtrer les matchs de la vague dont le tip-off est passé. Vérifier que les tests couvrent ce cas.

2. **Simulation complète** — Refaire `scripts/simulate_lot2_complete.py` depuis un instant où les 6 matchs sont tous à venir. Attendu : 6 collectes closing (une par match, chacune à H-0.25 de son tip-off), aucun snapshot post-tip-off. Coller sortie brute.

3. **`setup_cron.sh` → battement `*/20`** — Remplacer les 6 lignes de cron par 1 seule : `*/20 * * * *` (toutes les 20 min). Le collecteur calcule lui-même les cibles dues.

4. **README** — Documenter l'architecture auto-ordonnancée (tick 20 min, vagues 45 min, cibles configurables, priorités).

5. **Journal CLAUDE.md** — Ajouter entrée Lot 2 finalisé (bug corrigé, simulation OK, déploiement selon rituel).

6. **Déploiement** — Rituel : `docker compose build` + `up -d --force-recreate` + vérification (logs/base/Telegram).

---

## Fichiers clés

- `src/collector/collector.py` : Logique auto-ordonnancement (773 lignes)
- `src/collector/__main__.py` : Point d'entrée CLI
- `src/common/db.py` : Schéma `collection_log` (clé unique `match_id, target_name`)
- `tests/test_collector.py` : Suite de tests (201 tests)
- `config.yaml` : Cibles configurables (H-6, matin, H-3, verdict, redecision, closing)
- `scripts/setup_cron.sh` : Planning cron (à simplifier)
- `draft/architecture-collecteur-auto-ordonnance.md` : Spécification Lot 2

---

## Rappels .clinerules

- Interdiction de supprimer, skipper ou réécrire un test pour le faire passer
- Un bug identifié est signalé explicitement dans le rapport final, même non corrigé
- Un rapport final ne présente que la sortie brute de `pytest -q`
- Manquer de temps ou de contexte impose de s'arrêter et de rapporter l'état réel

---

**Prochaine action** : Corriger le bug snapshots post-tip-off, puis relancer la simulation complète.
