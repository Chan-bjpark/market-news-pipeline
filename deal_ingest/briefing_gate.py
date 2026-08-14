#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
briefing_gate.py — 뉴스 발송 전 "수집 완료" 신선도 게이트
------------------------------------------------------------------
목적: 발송(daily-news-slack-dm) 세션이 수집(orchestrator)보다 먼저 실행되어
      낡은 브리핑이 나가는 race condition을 원천 차단한다.

동작:
  1) logs/last_run_status.json 을 읽어 오늘(KST) 수집이 정상 완료됐는지 판정.
  2) 신선하면            -> "READY" 출력, exit 0  (발송 진행)
  3) 낡았/없으면          -> 수집을 직접 실행(run_orchestrator.ps1)하고 완료 대기.
                            단, 이미 다른 프로세스가 수집 중이면 중복 실행 없이 폴링만.
  4) 재수집 후에도 낡으면 -> "STALE_ABORT" + 사유 출력, exit 2  (발송 금지 → alert)

무결성 원칙: 추정·합성 없음. 판정 근거(파일 timestamp)만 사용.
이 스크립트는 사용자 로컬 PC에서 실행되므로 디스크 원본을 직접 읽는다(마운트 절단 무관).
"""
import json, sys, time, subprocess, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATUS = BASE / "logs" / "last_run_status.json"
WRAPPER = BASE / "run_orchestrator.ps1"

KST = datetime.timezone(datetime.timedelta(hours=9))

# --- 튜닝 파라미터 ---
RUN_TIMEOUT_SEC   = 360   # run_orchestrator.ps1 자체 실행 상한(재시도 포함)
POLL_TIMEOUT_SEC  = 300   # 타 프로세스가 수집 중일 때 대기 상한
POLL_INTERVAL_SEC = 15
INPROGRESS_WINDOW = 90    # last_run_status.json 이 이 초 이내 갱신 = 수집 진행 중으로 간주


def log(msg: str):
    ts = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    print(f"[gate] {ts} | {msg}", flush=True)


def _load_status():
    """last_run_status.json 로드. 부분쓰기 대비 salvage-parse 폴백 포함."""
    if not STATUS.exists():
        return None
    raw = STATUS.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        # 마지막 '}' 까지만 잘라 재시도(중간에 잘린 경우)
        cut = raw.rfind("}")
        if cut != -1:
            try:
                return json.loads(raw[: cut + 1])
            except Exception:
                return None
        return None


def _today_kst():
    return datetime.datetime.now(KST).date()


def freshness():
    """(is_fresh: bool, reason: str) 반환."""
    st = _load_status()
    if st is None:
        return False, "last_run_status.json 없음 또는 파싱 실패"
    if st.get("status") != "ok":
        return False, f"status={st.get('status')!r} (ok 아님)"
    if st.get("exit_code") not in (0, None):
        return False, f"exit_code={st.get('exit_code')!r}"

    files = st.get("files", {})
    rb = files.get("recent_briefing.json", {})
    if not rb.get("valid", False):
        return False, "recent_briefing.json valid=false"
    for fname, meta in files.items():
        if not meta.get("valid", False):
            return False, f"{fname} valid=false"

    gen = rb.get("generated_at")
    if not gen:
        return False, "recent_briefing.json generated_at 없음"
    try:
        dt = datetime.datetime.fromisoformat(gen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        gen_kst_date = dt.astimezone(KST).date()
    except Exception as e:
        return False, f"generated_at 파싱 실패: {gen!r} ({e})"

    today = _today_kst()
    if gen_kst_date != today:
        return False, f"수집 날짜 {gen_kst_date} != 오늘(KST) {today} — 낡은 데이터"
    return True, f"신선 (generated {dt.astimezone(KST):%Y-%m-%d %H:%M} KST, new_total={st.get('new_total')})"


def _run_in_progress():
    """다른 프로세스가 수집 중인지 추정: status 파일이 방금 갱신됐는가."""
    if not STATUS.exists():
        return False
    age = time.time() - STATUS.stat().st_mtime
    return age < INPROGRESS_WINDOW


def _launch_collection():
    log(f"수집 직접 실행: {WRAPPER}")
    if not WRAPPER.exists():
        log(f"경고: {WRAPPER} 없음 — orchestrator.py 직접 실행으로 폴백")
        py = BASE / "venv" / "Scripts" / "python.exe"
        exe = str(py) if py.exists() else "python"
        cmd = [exe, str(BASE / "orchestrator.py")]
    else:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(WRAPPER)]
    try:
        r = subprocess.run(cmd, cwd=str(BASE), timeout=RUN_TIMEOUT_SEC,
                           capture_output=True, text=True)
        log(f"수집 종료코드: {r.returncode}")
    except subprocess.TimeoutExpired:
        log(f"수집 타임아웃({RUN_TIMEOUT_SEC}s) — 계속 진행하여 상태 재확인")


def _poll_until_fresh(timeout_sec):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ok, reason = freshness()
        if ok:
            return True, reason
        time.sleep(POLL_INTERVAL_SEC)
    return freshness()


def main():
    ok, reason = freshness()
    if ok:
        log(f"READY — {reason}")
        print("READY")
        return 0

    log(f"신선하지 않음: {reason}")

    if _run_in_progress():
        log("수집이 이미 진행 중으로 판단 — 중복 실행 없이 대기(폴링)")
        ok, reason = _poll_until_fresh(POLL_TIMEOUT_SEC)
    else:
        _launch_collection()
        ok, reason = freshness()
        if not ok:
            # 방금 실행이 비동기/지연됐을 수 있으니 잠시 폴링
            ok, reason = _poll_until_fresh(POLL_TIMEOUT_SEC)

    if ok:
        log(f"READY(재수집 후) — {reason}")
        print("READY")
        return 0

    log(f"STALE_ABORT — 재수집 후에도 신선하지 않음: {reason}")
    print("STALE_ABORT")
    print(f"REASON: {reason}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
