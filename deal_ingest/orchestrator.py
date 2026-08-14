"""deal_ingest entry point. Run by Task Scheduler daily at 06:00 KST."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from store.db import Database  # noqa: E402
from collectors import (  # noqa: E402
    TheBellRssCollector, TheBellHtmlCollector, HankyungRssCollector,
    DealsiteHtmlCollector, InvestChosunHtmlCollector,
    MarketInsightHtmlCollector, EinfomaxHtmlCollector,
    NaverNewsApiCollector,
)
from util_io import atomic_write_json, validate_json_file  # noqa: E402


COLLECTOR_REGISTRY = {
    "thebell_rss": TheBellRssCollector,
    "thebell_html": TheBellHtmlCollector,
    "hankyung_rss": HankyungRssCollector,
    "dealsite_html": DealsiteHtmlCollector,
    "investchosun_html": InvestChosunHtmlCollector,
    "marketinsight_html": MarketInsightHtmlCollector,
    "einfomax_html": EinfomaxHtmlCollector,
    "naver_news_api": NaverNewsApiCollector,
}


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"ingest_{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("orchestrator")


def load_config(p):
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    _overlay_secrets_from_env(cfg)
    return cfg


def _overlay_secrets_from_env(cfg):
    """GitHub Actions/무인 실행용: 시크릿을 환경변수로 주입(config.json은 빈값 커밋).

    로컬 PC 실행 시엔 환경변수가 없으므로 config.json 값이 그대로 쓰인다(하위호환).
    env 값이 있으면 그것이 config.json 값을 덮어쓴다.
    """
    import os
    naver = cfg.get("collectors", {}).get("naver_news_api")
    if isinstance(naver, dict):
        if os.environ.get("NAVER_CLIENT_ID"):
            naver["client_id"] = os.environ["NAVER_CLIENT_ID"]
        if os.environ.get("NAVER_CLIENT_SECRET"):
            naver["client_secret"] = os.environ["NAVER_CLIENT_SECRET"]
    for section, envkey, field in (
        ("ecos", "ECOS_API_KEY", "api_key"),
        ("kfp", "KFP_SERVICE_KEY", "service_key"),
        ("fred", "FRED_API_KEY", "api_key"),
    ):
        sec = cfg.get(section)
        if isinstance(sec, dict) and os.environ.get(envkey):
            sec[field] = os.environ[envkey]


# _atomic_write_json has been moved to util_io.py (atomic_write_json) so it
# can be shared across orchestrator.py, fund_flow.py, ecos_rate.py without
# circular-import risk. The new implementation adds:
#   - os.fsync to defeat OS write-back cache (root cause of 2026-05-16 corruption)
#   - Post-write json.load validation to surface failures immediately
# Callers below now invoke atomic_write_json directly.


def run_collector(name, cfg, db, logger, dry_run=False):
    cls = COLLECTOR_REGISTRY.get(name)
    if not cls:
        logger.error("unknown collector: %s", name)
        return 0, 0
    coll = cls(cfg, logger=logging.getLogger(name))
    fetched = new = 0
    with db.run_log(name) as ctx:
        for art in coll.collect():
            fetched += 1
            if dry_run:
                logger.info("[dry] %s | %s | %s",
                            art.source_label, art.published_at[:19],
                            art.title[:70])
                continue
            if db.upsert_article(art):
                new += 1
        ctx.fetched_count = fetched
        ctx.new_count = new
    logger.info("%s: fetched=%d new=%d", name, fetched, new)
    return fetched, new


def export_briefing(db, days, out_path, logger):
    recent = db.recent_articles(days=days)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "count": len(recent),
        "articles": recent,
    }
    atomic_write_json(out_path, payload)
    logger.info("briefing export: %d -> %s", len(recent), out_path.name)


def run_rates(cfg, out_path, logger):
    """ECOS(CD91) + KFP(KTB 3Y/10Y) + FRED market close (US Treasury 2Y/10Y, USDKRW, WTI)
       + policy_rate (BOK & Fed) integration. Atomic write."""
    import ecos_rate, kfp_bond, policy_rate, fred_market
    merged = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": {},
        "market": {},
        "policy_rates": {},
    }
    ec_key = (cfg.get("ecos") or {}).get("api_key") if (cfg.get("ecos") or {}).get("enabled") else None
    kf_key = (cfg.get("kfp") or {}).get("service_key") if (cfg.get("kfp") or {}).get("enabled") else None
    fr_key = (cfg.get("fred") or {}).get("api_key") if (cfg.get("fred") or {}).get("enabled") else None
    sched = cfg.get("policy_schedule", {})
    bok_dates = sched.get("bok_2026", [])
    fomc_dates = sched.get("fomc_2026", [])

    if ec_key:
        try:
            ec = ecos_rate.run(ec_key, out_path.parent / "_ecos_tmp.json")
            for name, d in ec.get("items", {}).items():
                merged["items"][name] = {**d, "source": "ECOS"}
                if "error" in d:
                    logger.warning("ecos %s: %s", name, d["error"])
                else:
                    logger.info("ecos %s: %s%% on %s",
                                name, d["latest_value"], d["latest_date"])
        except Exception as e:
            logger.exception("ecos failed: %s", e)

    if kf_key:
        try:
            kf = kfp_bond.run(kf_key)
            for name, d in kf.items():
                merged["items"][name] = {**d, "source": "KFP/KRX"}
                if "error" in d:
                    logger.warning("kfp %s: %s", name, d["error"])
                else:
                    logger.info("kfp %s: %s%% on %s",
                                name, d["latest_value"], d["latest_date"])
        except Exception as e:
            logger.exception("kfp failed: %s", e)

    if fr_key:
        try:
            fm = fred_market.run(fr_key)
            for name, d in fm.items():
                if name in ("미국채(2년)", "미국채(10년)"):
                    merged["items"][name] = {
                        "latest_date": d.get("latest_date"),
                        "latest_value": d.get("latest_value"),
                        "prev_day_date": d.get("prev_day_date"),
                        "vs_prev_day_bp": d.get("vs_prev_day"),
                        "prev_month_date": d.get("prev_month_date"),
                        "vs_prev_month_bp": d.get("vs_prev_month"),
                        "recent_series": d.get("recent_series", []),
                        "source": "FRED",
                        "series_id": d.get("series_id"),
                        "error": d.get("error"),
                    }
                else:
                    merged["market"][name] = {**d}
                if "error" in d:
                    logger.warning("fred %s: %s", name, d["error"])
                else:
                    logger.info("fred %s: %s%s on %s (d %s%s, m %s%s)",
                                name, d.get("latest_value"), d.get("unit",""),
                                d.get("latest_date"),
                                d.get("vs_prev_day"), d.get("delta_unit",""),
                                d.get("vs_prev_month"), d.get("delta_unit",""))
        except Exception as e:
            logger.exception("fred_market failed: %s", e)

    if ec_key or fr_key:
        try:
            pr = policy_rate.run(ec_key, fr_key, bok_dates, fomc_dates)
            merged["policy_rates"] = pr.get("policy_rates", {})
            for name, d in merged["policy_rates"].items():
                if "error" in d:
                    logger.warning("policy %s: %s", name, d["error"])
                else:
                    val = d.get("latest_value_str") or d.get("latest_value")
                    logger.info("policy %s: %s on %s (last_change=%s direction=%s next=%s)",
                                name, val, d.get("latest_date"),
                                d.get("last_change_date"), d.get("direction"),
                                d.get("next_meeting"))
        except Exception as e:
            logger.exception("policy_rate failed: %s", e)

    atomic_write_json(out_path, merged)
    logger.info("rate_summary export: %d items + %d market + %d policy_rates -> %s",
                len(merged["items"]), len(merged["market"]),
                len(merged["policy_rates"]), out_path.name)


def run_fund_flow(cfg, db_path, out_path, logger):
    """Policy-fund lifecycle tracker -> fund_flow_summary.json."""
    import fund_flow
    try:
        lookback = cfg.get("fund_flow", {}).get("lookback_days", 90)
        result = fund_flow.run(db_path, out_path, days_back=lookback)
        logger.info("fund_flow export: %d funds, %d policy articles -> %s",
                    result["matched_funds_count"],
                    result["total_policy_articles"],
                    out_path.name)
    except Exception as e:
        logger.exception("fund_flow failed: %s", e)


def run_kgf_execution(cfg, db_path, out_path, logger):
    """국민성장펀드 집행(개별 기업 자금 집행) 누적 트래커 -> kgf_execution_summary.json.

    fund_flow(펀드 조성 단계)와 분리. 90일 DB 윈도우를 스캔하되 기존 JSON을
    병합 누적하므로 retention purge 후에도 과거 집행 deal 이 보존된다.
    비핵심 export 이므로 실패해도 run 전체를 fail 시키지 않는다(health check 대상 아님).
    """
    import kgf_execution
    try:
        lookback = cfg.get("kgf_execution", {}).get("lookback_days", 90)
        result = kgf_execution.run(db_path, out_path, days_back=lookback)
        logger.info("kgf_execution export: %d deals (this-run matched %d) -> %s",
                    result.get("deals_count", 0),
                    result.get("matched_articles_this_run", 0),
                    out_path.name)
    except Exception as e:
        logger.exception("kgf_execution failed: %s", e)


def cmd_status(db_path):
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return
    with Database(db_path) as db:
        conn = db._conn
        print("=" * 60)
        print("Articles by source (cumulative)")
        print("=" * 60)
        rows = conn.execute(
            "SELECT source_label, COUNT(*) AS n, MAX(published_at) AS latest "
            "FROM articles GROUP BY source ORDER BY n DESC"
        ).fetchall()
        if not rows:
            print("  (no articles)")
        for r in rows:
            print(f"  {r['source_label']:15} {r['n']:6} | latest: {r['latest'][:19]}")
        print()
        print("=" * 60)
        print("Articles in last 3 days by category")
        print("=" * 60)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM articles "
            "WHERE published_at >= ? GROUP BY category ORDER BY n DESC",
            (cutoff,),
        ).fetchall()
        if not rows:
            print("  (no recent)")
        for r in rows:
            print(f"  {r['category'] or '(none)':12} {r['n']:4}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "config.json"))
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    db_path = SCRIPT_DIR / cfg["storage"]["db_path"]
    log_dir = SCRIPT_DIR / cfg["storage"]["log_dir"]
    if args.status:
        cmd_status(db_path)
        return
    logger = setup_logging(log_dir)
    logger.info("=" * 50)
    logger.info("deal_ingest start")
    tot_f = tot_n = 0
    with Database(db_path) as db:
        for name, c in cfg["collectors"].items():
            if name.startswith("_"):
                continue
            if not c.get("enabled"):
                logger.info("%s: disabled", name)
                continue
            if args.only and args.only != name:
                continue
            try:
                f, n = run_collector(name, c, db, logger, dry_run=args.dry_run)
                tot_f += f
                tot_n += n
            except Exception as e:
                logger.exception("%s: crashed: %s", name, e)
        if not args.dry_run:
            ret = cfg["storage"].get("retention_days", 90)
            removed = db.purge_old(ret)
            if removed:
                logger.info("retention: %d purged", removed)
            days = cfg.get("filter", {}).get("recency_days", 3)
            export_briefing(db, days, SCRIPT_DIR / "recent_briefing.json", logger)
            try:
                run_rates(cfg, SCRIPT_DIR / "rate_summary.json", logger)
            except Exception as e:
                logger.exception("rates: %s", e)
            try:
                run_fund_flow(cfg, db_path,
                              SCRIPT_DIR / "fund_flow_summary.json", logger)
            except Exception as e:
                logger.exception("fund_flow: %s", e)
            try:
                run_kgf_execution(cfg, db_path,
                                  SCRIPT_DIR / "kgf_execution_summary.json", logger)
            except Exception as e:
                logger.exception("kgf_execution: %s", e)
    logger.info("deal_ingest done: fetched=%d new=%d", tot_f, tot_n)

    # End-of-run health check (validates the 3 critical exports)
    # On failure: write logs/HEALTH_FAIL_<ts>.flag + last_run_status.json
    # so the downstream daily-news-slack-dm task picks it up and alerts.
    if not args.dry_run:
        exit_code = _final_health_check(SCRIPT_DIR, log_dir, logger,
                                        fetched=tot_f, new=tot_n)
        if exit_code != 0:
            sys.exit(exit_code)


def _final_health_check(script_dir, log_dir, logger, fetched=0, new=0):
    """Validate the 3 export files. Write last_run_status.json + flag.

    Returns exit code (0=all_ok, 2=at_least_one_corrupt).
    """
    targets = {
        "recent_briefing.json": script_dir / "recent_briefing.json",
        "rate_summary.json": script_dir / "rate_summary.json",
        "fund_flow_summary.json": script_dir / "fund_flow_summary.json",
    }
    file_status = {}
    all_ok = True
    for name, path in targets.items():
        ok, err, size = validate_json_file(path)
        gen_at = None
        if ok:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    gen_at = json.load(f).get("generated_at")
            except Exception:
                pass
        file_status[name] = {
            "valid": ok,
            "size": size,
            "error": err if not ok else None,
            "generated_at": gen_at,
        }
        if ok:
            logger.info("health_check OK | %s | %d bytes | gen=%s",
                        name, size, gen_at)
        else:
            logger.error("health_check FAIL | %s | %s", name, err)
            all_ok = False

    now_utc = datetime.now(timezone.utc).isoformat()
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime(
        "%Y-%m-%d %H:%M:%S KST")
    status_payload = {
        "run_at_utc": now_utc,
        "run_at_kst": now_kst,
        "fetched_total": fetched,
        "new_total": new,
        "status": "ok" if all_ok else "corrupt",
        "exit_code": 0 if all_ok else 2,
        "files": file_status,
    }

    # last_run_status.json — overwritten every run (atomic so downstream sees consistent view)
    status_path = log_dir / "last_run_status.json"
    try:
        atomic_write_json(status_path, status_payload, validate=False)
        logger.info("last_run_status written: %s", status_path.name)
    except Exception as e:
        logger.exception("failed to write last_run_status: %s", e)

    # Flag file on failure for high-visibility detection
    if not all_ok:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        flag = log_dir / f"HEALTH_FAIL_{ts}.flag"
        try:
            flag.write_text(
                json.dumps(status_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.error("HEALTH_FAIL flag written: %s", flag.name)
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    main()
