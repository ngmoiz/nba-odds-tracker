#!/usr/bin/env python3
"""Simulation Lot 2 COMPLÈTE : 6 matchs WNBA, verdicts insérés, closing per-match.

Déroulé complet sur 24h avec insertion des verdicts au bon moment pour tester
la cible closing (H-0.25 par match, marché du verdict).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def get_odds(self, markets: list[str] | None = None) -> list[OddsEvent]:
        """Renvoie 6 matchs avec tip-offs échelonnés (vagues multiples)."""
        # 6 matchs : 3 vagues (m1-m2 à 30 min, m3-m4 à 30 min, m5-m6 à 30 min)
        # Écart inter-vagues : 60 min (> seuil 45 min)
        # Tip-offs échelonnés sur 3.5h à partir de H+12 (pour avoir toute la fenêtre)
        matches = [
            ("m1", "Sky", "Storm", self.now + timedelta(hours=12)),
            ("m2", "Aces", "Sparks", self.now + timedelta(hours=12.5)),
            ("m3", "Liberty", "Sun", self.now + timedelta(hours=13.5)),
            ("m4", "Mystics", "Fever", self.now + timedelta(hours=14)),
            ("m5", "Wings", "Dream", self.now + timedelta(hours=15)),
            ("m6", "Mercury", "Lynx", self.now + timedelta(hours=15.5)),
        ]
        
        events = []
        for match_id, home, away, tipoff in matches:
            all_markets = [
                Market("h2h", [Outcome(home, 1.74, None), Outcome(away, 2.14, None)]),
                Market("spreads", [Outcome(home, 1.93, -2.5), Outcome(away, 1.89, 2.5)]),
                Market("totals", [Outcome("Over", 1.91, 165.5), Outcome("Under", 1.91, 165.5)]),
            ]
            
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
    db_path = Path("/tmp/nba_simulation_lot2_complete.db")
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
    
    # Point de départ : maintenant (tip-offs dans 6h)
    start_time = datetime.now(timezone.utc)
    
    print("=" * 80)
    print("SIMULATION LOT 2 COMPLÈTE : 6 matchs WNBA, 90 ticks sur 30h")
    print("=" * 80)
    print(f"Début : {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()
    print("Tip-offs :")
    print(f"  m1 : {(start_time + timedelta(hours=12)).strftime('%H:%M')}")
    print(f"  m2 : {(start_time + timedelta(hours=12.5)).strftime('%H:%M')}")
    print(f"  m3 : {(start_time + timedelta(hours=13.5)).strftime('%H:%M')}")
    print(f"  m4 : {(start_time + timedelta(hours=14)).strftime('%H:%M')}")
    print(f"  m5 : {(start_time + timedelta(hours=15)).strftime('%H:%M')}")
    print(f"  m6 : {(start_time + timedelta(hours=15.5)).strftime('%H:%M')}")
    print()
    
    # 90 ticks (1 tick toutes les 20 min sur 30h)
    tick_interval = timedelta(minutes=20)
    total_credits = 0
    verdicts_created = False
    
    for tick in range(90):
        tick_time = start_time + (tick * tick_interval)
        
        client = SimulationClient(tick_time)
        
        # Force la première collecte pour découvrir les matchs immédiatement
        force_collect = (tick == 0)
        
        summary = run_collection(
            conn,
            client,
            "basketball_wnba",
            config,
            force=force_collect,
            now=tick_time,
        )
        
        if not summary.get("skipped"):
            total_credits += int(client.last_request_cost)
            
            print(f"Tick {tick:2d} | {tick_time.strftime('%H:%M')} | "
                  f"Vagues: {summary.get('waves', 0)} | "
                  f"Cibles: {summary.get('targets_collected', 0)} | "
                  f"Snapshots: {summary.get('snapshots', 0)} | "
                  f"Crédits: +{client.last_request_cost}")
        
        # Insère les verdicts immédiatement après la découverte forcée (tick 0)
        if tick == 0 and not verdicts_created:
            match_count = conn.execute("SELECT COUNT(*) as cnt FROM matches").fetchone()["cnt"]
            if match_count == 6:
                # Passe les matchs en DECIDE et crée les verdicts
                # Verdicts alternés : m1/m3/m5 sur h2h, m2/m4/m6 sur spreads
                verdict_time = tick_time
                
                for match_id in ["m1", "m2", "m3", "m4", "m5", "m6"]:
                    db.update_match_status(conn, match_id, "DECIDE")
                
                db.insert_verdict(conn, match_id="m1", verdict="BET", selection="Sky", market="h2h", line=None, odds_at_verdict=1.74, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                db.insert_verdict(conn, match_id="m2", verdict="BET", selection="Aces", market="spreads", line=-2.5, odds_at_verdict=1.93, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                db.insert_verdict(conn, match_id="m3", verdict="BET", selection="Liberty", market="h2h", line=None, odds_at_verdict=1.74, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                db.insert_verdict(conn, match_id="m4", verdict="BET", selection="Mystics", market="spreads", line=-2.5, odds_at_verdict=1.93, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                db.insert_verdict(conn, match_id="m5", verdict="BET", selection="Wings", market="h2h", line=None, odds_at_verdict=1.74, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                db.insert_verdict(conn, match_id="m6", verdict="BET", selection="Mercury", market="spreads", line=-2.5, odds_at_verdict=1.93, signal_score=10, rules_triggered="[]", rationale="sim", decided_at=verdict_time.isoformat())
                conn.commit()
                verdicts_created = True
                print(f"Tick {tick:2d} | {tick_time.strftime('%H:%M')} | → Verdicts créés (h2h/spreads alternés), matchs DECIDE")
    
    print()
    print("=" * 80)
    print("RÉSULTATS DÉTAILLÉS")
    print("=" * 80)
    
    # Collection log
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
    
    # Doublons
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
    
    print(f"\n📸 Snapshots par match :")
    for s in snapshots:
        print(f"  {s['match_id']} : {s['cnt']} snapshots")
    
    # Crédits
    print(f"\n💰 Crédits consommés : {total_credits}")
    print(f"   Collectes API : {total_credits // 3}")
    
    # Closing per-match (CRITIQUE)
    print(f"\n🎯 Vérification closing per-match (CRITIQUE) :")
    closing_logs = conn.execute("""
        SELECT match_id, markets, collected_at
        FROM collection_log
        WHERE target_name = 'closing'
        ORDER BY match_id
    """).fetchall()
    
    if closing_logs:
        print(f"   {len(closing_logs)} collectes closing (attendu : 6)")
        for log in closing_logs:
            print(f"  {log['match_id']} : {log['markets']:15s} | {log['collected_at']}")
    else:
        print("  ⚠️  AUCUNE collecte closing (BUG)")
    
    # Snapshots post-tip-off (garde)
    print(f"\n🚫 Snapshots post-tip-off (garde) :")
    post_tipoff = conn.execute("""
        SELECT m.match_id, m.tipoff_utc, COUNT(o.id) as cnt
        FROM matches m
        LEFT JOIN odds_snapshots o ON m.match_id = o.match_id
        WHERE o.snapshot_at > m.tipoff_utc
        GROUP BY m.match_id
        HAVING cnt > 0
    """).fetchall()
    
    if post_tipoff:
        print(f"  ⚠️  {len(post_tipoff)} matchs avec snapshots post-tip-off (BUG)")
        for p in post_tipoff:
            print(f"    {p['match_id']} : {p['cnt']} snapshots après {p['tipoff_utc']}")
    else:
        print("  ✅ Aucun snapshot post-tip-off (garde OK)")
    
    conn.close()
    print()
    print("=" * 80)
    print("✅ Simulation terminée")
    print("=" * 80)


if __name__ == "__main__":
    main()
