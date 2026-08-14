# Phase 2 — 발송 세션 데이터 출처 전환 (검증 통과 후에만 적용)

> ⚠️ **지금 적용하지 말 것.** Phase 2 병행운영에서 신규(클라우드) 수집이 5영업일 동등성 검증을
> 통과한 뒤, 발송지침(CEO_뉴스브리핑_발송지침.md)의 데이터 출처를 아래처럼 바꾼다.
> 그 전까지 발송은 기존 PC 경로(device bridge)로 그대로 유지한다.

`<RAW_BASE>` = `https://raw.githubusercontent.com/<GitHub계정>/market-news-pipeline/main/deal_ingest`
(bootstrap_deploy.ps1 실행 후 마지막에 출력되는 실제 URL로 치환)

## 바꿀 지점 (발송지침 B절·C절)

### B절 데이터 소스 경로 표 — 전(前)
```
사용자 PC의 deal_ingest 파이프라인이 매일 06:00 KST 수집. 기본 경로:
C:\Users\lg\Documents\Claude\Projects\뉴스 정기발송\deal_ingest\
| recent_briefing.json | 수집 기사 |
...
```
### B절 — 후(後)
```
수집은 GitHub Actions가 매 평일 06:00 KST 실행(PC 무관). 데이터는 아래 raw URL에서 WebFetch로 읽는다.
| <RAW_BASE>/recent_briefing.json | 수집 기사 |
| <RAW_BASE>/rate_summary.json | 금리·시장 |
| <RAW_BASE>/fund_flow_summary.json | 펀드 조성 트래커 |
| <RAW_BASE>/kgf_execution_summary.json | KGF 집행 트래커 |
| <RAW_BASE>/logs/last_run_status.json | 수집 health |
```

### C절 0.1 수집완료 게이트 — 변경점
- `last_run_status.json`·`recent_briefing.json`을 **raw URL WebFetch**로 읽는다(로컬 Read 대신).
- freshness 판정(‘generated_at의 KST 날짜 == 오늘’)은 **동일 로직**.
- ‘오늘 수집 미완료’ 시 조치 문구를 PC PowerShell 실행 대신 **“GitHub Actions에서 collect 워크플로 수동 실행(Run workflow)”**으로 교체.
- briefing_gate.py(로컬 폴링) 경로는 제거하고, GitHub Actions 실패 시 재실행 안내로 대체.

### C절 4-B KGF — 변경점
- Chrome MCP 대시보드 조회 문단은 **참고용으로 강등**한다. KGF aggregate는 이미 수집단계(`kgf_sheet_sync.py`, 시트 HTTP)에서 확정되어 `kgf_execution_summary.json`에 반영되므로, 발송은 그 값을 그대로 쓴다.
- 시트 fetch 실패(aggregate_source="상수 fallback")가 `last_run_status`/JSON에 보이면, 그때만 예외적으로 Chrome MCP 조회(단, 완전무인화 목표상 시트 공유설정 유지가 정석).

### 워치리스트(distressed_watchlist.json) — 주의
- 공개 리포에 커밋하지 않으므로 raw에 없음. 발송 세션의 한계기업 섹션 watchlist read/write는 **당분간 기존 방식 유지**(별도 처리). Phase 3에서 비공개 저장 채널을 정한다.

## 롤백
문제 시 B절 경로를 다시 PC 로컬 경로로 되돌리고 PC Task Scheduler를 재활성화하면 즉시 원복.
