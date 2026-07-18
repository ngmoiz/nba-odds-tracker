#!/usr/bin/env python3
"""Simulation Lot 2 : 6 matchs WNBA, 72 ticks sur 24h.

Vérifie :
- Groupement en vagues (seuil 45 min)
- Cibles servies par match (déduplication par target_name)
- Collection_log (zéro doublon)
- Crédits consommés
- Closing per-match (marché du verdict)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ajoute src/ au PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collector.collector import run_collection
from common import db
from common.odds_api_client import Bookmaker, Market, OddsEvent, Outcome


class SimulationClient:
    """Client simulé : 6 matchs WNBA avec tip-offs échelonnés."""

    def __init__(self, now: datetime):
        self.now = now
        self.credits_remaining = "500"
        self.last_request_cost = "3"
        self.call_count = 0

    def get_odds(self, markets: list[str] | None = None) -> list[OddsEvent]:
        """Renvoie 6 matchs avec tip-offs échelonnés (vagues multiples)."""
        self.call_count += 1
        
        # 6 matchs : 3 vagues (m1-m2 à 30 min, m3-m4 à 30 min, m5-m6 à 30 min)
        # Écart inter-vagues : 60 min (> seuil 45 min)
        matches = [
            ("m1", "Sky", "Storm", self.now + timedelta(hours=6)),
            ("m2", "Aces", "Sparks", self.now + timedelta(hours=6.5)),
            ("m3", "Liberty", "Sun", self.now + timedelta(hours=7.5)),
            ("m4", "Mystics", "Fever", self.now + timedelta(hours=8)),
            ("m5", "Wings", "Dream", self.now + timedelta(hours=9)),
            ("m6", "Mercury", "Lynx", self.now + timedelta(hours=9.5)),
        ]
        
        events = []
        for match_id, home, away, tipoff in matches:
            all_markets = [
                Market("h2h", [Outcome(home, 1.74, None), Outcome(away, 2.14, None)]),
                Market("spreads", [Outcome(home, 1.93, -2.5), Outcome(away, 1.89, 2.5)]),
                Market("totals", [Outcome("Over", 1.91, 165.5), Outcome("Under", 1.91, 165.5)]),
            ]
            
            # Filtre les marchés si demandé
            if markets is not None:
                all_markets = [m for m in all_markets if m.key in markets]
            
            events.append(
                OddsEvent(
                    id=match_id,
                    sport_key="basketball_wnba",
                    commence_time=tipoff.isoformat(),
                    home_team=home,
                    away_team=away,
                    bookmakers=[
                        Bookmaker(
                            key="draftkings",
                            title="DraftKings",
                            last_update=self.now.isoformat(),
                            markets=all_markets,
                        )
                    ],
                )
            )
        
        return events


def main():
    """Simulation complète sur 24h."""
    # Base temporaire
    db_path = Path("/tmp/nba_simulation_lot2.db")
    if db_path.exists():
        db_path.unlink()
    
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    
    # Config Lot 2
    config = {
        "quota": {"reserve": 50},
        "collector": {
            "wave_grouping_minutes": 45,
            "targets": [
                {"name": "H-6", "hours_before": 6.0, "markets": ["h2h"], "priority": 2},
                {"name": "verdict", "hours_before": 2.0, "markets": ["h2h", "spreads"], "priority": 1},
                {"name": "redecision", "hours_before": 1.0, "markets": ["h2h", "spreads"], "priority": 1},
                {"name": "closing", "hours_before": 0.25, "per_match": True, "priority": 1},
            ],
        },
    }
    
    # Point de départ : H-12 (12h avant le 1er match)
    start_time = datetime.now(timezone.utc)
    
    print("=" * 80)
    print("SIMULATION LOT 2 : 6 matchs WNBA, 72 ticks sur 24h")
    print("=" * 80)
    print()
    
    # 72 ticks (1 tick toutes les 20 min sur 24h)
    tick_interval = timedelta(minutes=20)
    total_credits = 0
    
    # Crée des verdicts pour les 6 matchs (pour tester closing per-match)
    # Verdicts créés à H-2.5 (juste avant la fenêtre verdict H-2)
    
    for tick in range(72):
        tick_time = start_time + (tick * tick_interval)
        client = SimulationClient(tick_time)
        
        # Collecte conditionnelle (force=False)
        summary = run_collection(
            conn,
            client,
            "basketball_wnba",
            config,
            force=False,
            now=tick_time,
        )
        
        if not summary.get("skipped"):
            total_credits += int(client.last_request_cost)
            
            print(f"Tick {tick:2d} | {tick_time.strftime('%H:%M')} | "
                  f"Vagues: {summary.get('waves', 0)} | "
                  f"Cibles: {summary.get('targets_collected', 0)} | "
                  f"Snapshots: {summary.get('snapshots', 0)} | "
                  f"Crédits: +{client.last_request_cost}")
    
    print()
    print("=" * 80)
    print("RÉSULTATS")
    print("=" * 80)
    
    # Statistiques collection_log
    logs = conn.execute("""
        SELECT match_id, target_name, target_hours, markets, credits_used, collected_at
        FROM collection_log
        ORDER BY collected_at
    """).fetchall()
    
    print(f"\n📊 Collection log : {len(logs)} entrées")
    print()
    for log in logs:
        print(f"  {log['match_id']} | {log['target_name']:10s} | "
              f"H-{log['target_hours']:4.2f} | {log['markets']:20s} | "
              f"Crédits: {log['credits_used']}")
    
    # Vérification doublons
    doublons = conn.execute("""
        SELECT match_id, target_name, COUNT(*) as cnt
        FROM collection_log
        GROUP BY match_id, target_name
        HAVING cnt > 1
    """).fetchall()
    
    print(f"\n🔍 Doublons : {len(doublons)}")
    if doublons:
        for d in doublons:
            print(f"  ⚠️  {d['match_id']} | {d['target_name']} : {d['cnt']} fois")
    
    # Snapshots par match
    snapshots = conn.execute("""
        SELECT match_id, COUNT(*) as cnt
        FROM odds_snapshots
        GROUP BY match_id
        ORDER BY match_id
    """).fetchall()
    
    print("\n📸 Snapshots par match :")
    for s in snapshots:
        print(f"  {s['match_id']} : {s['cnt']} snapshots")
    
    # Crédits totaux
    print(f"\n💰 Crédits consommés : {total_credits}")
    
    # Vérification closing per-match
    print("\n🎯 Vérification closing per-match :")
    closing_logs = conn.execute("""
        SELECT match_id, markets
        FROM collection_log
        WHERE target_name = 'closing'
        ORDER BY match_id
    """).fetchall()
    
    for log in closing_logs:
        print(f"  {log['match_id']} : {log['markets']}")
    
    conn.close()
    print()
    print("=" * 80)
    print("✅ Simulation terminée")
    print("=" * 80)


if __name__ == "__main__":
    main()
