#!/usr/bin/env python3
"""
Dismiss Review Report
=====================

Generates a report of IdeaForge ideas dismissed by Claude's classifier
for human review. Surfaces potentially-valuable ideas that may have been
incorrectly dismissed, especially developer tools where pain is diffuse.

Usage:
    python scripts/dismiss_review.py
    python scripts/dismiss_review.py --min-score 5.0
    python scripts/dismiss_review.py --top 10
"""
import argparse
import sqlite3
from pathlib import Path


IDEAFORGE_DB = Path(__file__).parent.parent.parent / "ideaforge" / "data" / "ideaforge.db"


def main():
    parser = argparse.ArgumentParser(description="Dismiss Review Report")
    parser.add_argument("--min-score", type=float, default=4.5, help="Minimum weighted_score to include (default: 4.5)")
    parser.add_argument("--top", type=int, default=0, help="Show only top N ideas by score (0=all)")
    args = parser.parse_args()

    if not IDEAFORGE_DB.exists():
        print(f"IdeaForge DB not found at {IDEAFORGE_DB}")
        return 1

    conn = sqlite3.connect(str(IDEAFORGE_DB))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT id, title, description, weighted_score, artifact_type,
               route_rationale, struggling_user, claimed_by
        FROM ideas
        WHERE status = 'dismissed'
          AND weighted_score >= ?
        ORDER BY weighted_score DESC
    """
    params = [args.min_score]

    if args.top:
        query += " LIMIT ?"
        params.append(args.top)

    rows = conn.execute(query, params).fetchall()

    # Stats
    total_dismissed = conn.execute("SELECT COUNT(*) FROM ideas WHERE status = 'dismissed'").fetchone()[0]
    total_ideas = conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]
    avg_score = conn.execute("SELECT AVG(weighted_score) FROM ideas WHERE status = 'dismissed'").fetchone()[0]

    print("=" * 80)
    print("DISMISS REVIEW REPORT")
    print("=" * 80)
    print(f"\nDismissed: {total_dismissed}/{total_ideas} ({round(total_dismissed/total_ideas*100)}%)")
    print(f"Average dismissed score: {avg_score:.1f}/10")
    print(f"Showing: {len(rows)} ideas with score >= {args.min_score}")
    print()

    # Categorize dismiss reasons
    cannot_identify = 0
    threshold_forced = 0
    other = 0

    for r in rows:
        user = r["struggling_user"] or ""
        if "cannot" in user.lower() or "unable" in user.lower() or not user:
            cannot_identify += 1
        elif r["artifact_type"] == "dismiss":
            threshold_forced += 1
        else:
            other += 1

    print(f"Dismiss reasons: {cannot_identify} 'cannot identify user', {threshold_forced} threshold-forced, {other} other")
    print("-" * 80)

    for r in rows:
        score = round(r["weighted_score"] * 10, 1)
        claimed = " [CLAIMED]" if r["claimed_by"] else ""
        print(f"\n  ID:{r['id']} Score={score} Type={r['artifact_type']}{claimed}")
        print(f"  Title: {r['title']}")
        if r["description"]:
            desc = r["description"][:120] + "..." if len(r["description"] or "") > 120 else r["description"]
            print(f"  Desc:  {desc}")
        if r["struggling_user"]:
            user = r["struggling_user"][:100] + "..." if len(r["struggling_user"] or "") > 100 else r["struggling_user"]
            print(f"  User:  {user}")
        if r["route_rationale"]:
            rationale = r["route_rationale"][:120] + "..." if len(r["route_rationale"] or "") > 120 else r["route_rationale"]
            print(f"  Why:   {rationale}")

    print()
    print("-" * 80)
    print("Action: Review ideas above and decide which to rescue (change status to 'classified' in IdeaForge)")

    conn.close()
    return 0


if __name__ == "__main__":
    exit(main())
