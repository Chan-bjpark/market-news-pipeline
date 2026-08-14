"""병행운영 검증 — PC 산출물 vs 클라우드(리포) 산출물 동등성 비교.

사용:
    python verify_parallel.py <PC_recent_briefing.json> <CLOUD_recent_briefing.json>
    # rate_summary 도 비교하려면:
    python verify_parallel.py <pc_rb> <cloud_rb> --rate <pc_rate> <cloud_rate>

목적: 신규(클라우드) 수집이 기존(PC) 대비 동등 이상인지 —
      소스별·카테고리별 건수, hlb_signal 건수 — 를 매 영업일 확인.
데이터 무결성: 실제 파일만 비교. 없는 값은 만들지 않음.
"""
from __future__ import annotations
import json, sys
from collections import Counter


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def profile(rb):
    arts = rb.get("articles", [])
    by_src = Counter(a.get("source_label", "?") for a in arts)
    by_cat = Counter(a.get("category") or "other" for a in arts)
    hlb = sum(1 for a in arts if (a.get("category") == "hlb_signal"))
    return len(arts), by_src, by_cat, hlb, rb.get("generated_at")


def show_cmp(name, a, b):
    keys = sorted(set(a) | set(b))
    print(f"\n[{name}]  (PC → CLOUD)")
    for k in keys:
        pa, pb = a.get(k, 0), b.get(k, 0)
        flag = "" if pb >= pa else "  ⚠️ 클라우드 적음"
        print(f"  {k:16} {pa:4} → {pb:4}{flag}")


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    pc, cloud = load(sys.argv[1]), load(sys.argv[2])
    pn, ps, pc_cat, ph, pg = profile(pc)
    cn, cs, cc_cat, ch, cg = profile(cloud)
    print("=" * 56)
    print("recent_briefing 비교")
    print("=" * 56)
    print(f"generated_at   PC={pg}  CLOUD={cg}")
    print(f"총 기사        PC={pn}  CLOUD={cn}  {'OK' if cn >= pn*0.9 else '⚠️ 10%+ 적음'}")
    print(f"hlb_signal     PC={ph}  CLOUD={ch}  {'OK' if ch >= ph else '⚠️ 신호 누락 점검'}")
    show_cmp("소스별", ps, cs)
    show_cmp("카테고리별", pc_cat, cc_cat)

    if "--rate" in sys.argv:
        i = sys.argv.index("--rate")
        rp, rc = load(sys.argv[i + 1]), load(sys.argv[i + 2])
        pi = set((rp.get("items") or {}).keys())
        ci = set((rc.get("items") or {}).keys())
        print("\n" + "=" * 56)
        print("rate_summary items 비교")
        print("=" * 56)
        print(f"  PC items:    {sorted(pi)}")
        print(f"  CLOUD items: {sorted(ci)}")
        miss = pi - ci
        print(f"  누락: {sorted(miss) if miss else '없음 (OK)'}")

    print("\n판정 가이드: 소스별·카테고리별에서 클라우드가 PC 대비 크게 적은 항목이")
    print("없고, hlb_signal 이 동등 이상이면 동등성 통과. HTML 매체가 0이면 그 매체가")
    print("데이터센터 IP에서 차단된 것 — config.json enabled:false 또는 WebSearch 보완.")


if __name__ == "__main__":
    main()
