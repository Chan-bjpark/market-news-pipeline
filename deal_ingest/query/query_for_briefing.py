"""
Cowork 뉴스 task가 호출할 조회 인터페이스.

매일 07:00 KST 뉴스 task가 이 스크립트를 실행하여 최근 3일 딜·M&A 기사를 가져온다.

Usage:
    python query_for_briefing.py                # 기본: 최근 3일, 전체 매체, JSON 출력
    python query_for_briefing.py --days 7       # 최근 7일
    python query_for_briefing.py --source thebell  # 특정 매체만
    python query_for_briefing.py --status       # 상태 요약 (사람용)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from store.db import Database  # noqa: E402


def load_db_path() -> Path:
    config_path = PROJECT_DIR / "config.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        return PROJECT_DIR / cfg["storage"]["db_path"]
    return PROJECT_DIR / "store" / "articles.db"


def query_recent(days: int, sources: list[str] | None = None,
                 categories: list[str] | None = None) -> list[dict]:
    db_path = load_db_path()
    if not db_path.exists():
        return []
    with Database(db_path) as db:
        return db.recent_articles(days=days, sources=sources, categories=categories)


def main():
    parser = argparse.ArgumentParser(description="Query deal_ingest DB for briefing")
    parser.add_argument("--days", type=int, default=3,
                        help="최근 N일 (기본 3)")
    parser.add_argument("--source", action="append",
                        help="특정 매체만 (반복 가능). 예: --source thebell --source dealsite")
    parser.add_argument("--category", action="append",
                        help="특정 카테고리만. M&A | PE | fund_raise | lp_commit | cap_market | other")
    parser.add_argument("--status", action="store_true",
                        help="사람이 읽을 수 있는 요약 형태로 출력")
    args = parser.parse_args()

    rows = query_recent(days=args.days, sources=args.source, categories=args.category)

    if args.status:
        if not rows:
            print(f"최근 {args.days}일 채택 가능 기사: 0건")
            return
        print(f"최근 {args.days}일 채택 가능 기사: {len(rows)}건")
        print()
        by_source = {}
        for r in rows:
            by_source.setdefault(r["source_label"], []).append(r)
        for src, items in sorted(by_source.items(), key=lambda x: -len(x[1])):
            print(f"  {src}: {len(items)}건")
        print()
        print("최근 10건:")
        for r in rows[:10]:
            print(f"  [{r['published_at'][:10]}] [{r['category'] or '?':10}] "
                  f"{r['source_label']:8} | {r['title'][:60]}")
    else:
        # JSON — Cowork 뉴스 task가 직접 파싱
        print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
