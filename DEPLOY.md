# deal_ingest 무인화 배포 — 작업지시서 (GitHub Actions)

> 목표: 뉴스 수집을 **PC 온·오프와 무관하게** GitHub Actions에서 매 평일 06:00 KST 실행하고,
> 산출물을 리포에 커밋해 발송 세션이 클라우드에서 읽게 한다. **기존 PC 발송은 검증 완료 전까지 그대로 유지**하고,
> 신규 파이프라인이 N영업일 연속 정상 확인되면 PC 작업을 폐기한다.

---

## 0. 아키텍처 (before → after)

| 단계 | 현재(PC 의존) | 무인화 후 |
|---|---|---|
| 수집 실행 | PC Windows Task Scheduler 06:00 → `orchestrator.py` | **GitHub Actions cron** `0 21 * * 0-4`(UTC)=평일 06:00 KST |
| 데이터 저장 | PC 로컬 디스크 | **리포에 커밋**(actions/cache로 DB 유지) |
| KGF 집행 수치 | 시트 동기화 or 발송세션 Chrome MCP | `kgf_sheet_sync.py`(HTTP)로 수집단계 확정 → **Chrome 불필요** |
| 발송 데이터 접근 | 발송세션이 device bridge로 PC 읽기 | 발송세션이 **GitHub raw URL** fetch |
| 발송 실행 | Cowork 스케줄 세션(클라우드) | 동일(그대로) — 데이터 출처만 전환 |

cron 시각 환산: KST = UTC+9. 06:00 KST 평일(월~금) = 21:00 UTC 일~목 → `0 21 * * 0-4`.
발송(07:00 KST)은 기존 CCR 트리거 유지.

---

## Phase 1 — GitHub 셋업 (사용자 작업)

> **가장 빠른 길 — 원클릭 스크립트**: 전제(Git·GitHub CLI 설치 후 `gh auth login`)만 갖추면,
> 이 패키지를 푼 폴더에서 아래 한 줄이면 리포 생성·시크릿 등록·seed·첫 실행까지 끝난다.
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\bootstrap_deploy.ps1
> ```
> 스크립트가 기존 `deal_ingest\config.json`에서 실 키를, 같은 폴더에서 seed를 자동으로 읽어 처리한다.
> 수동으로 하려면 1-1~1-4를 따른다.

### 1-1. 리포 생성 — **공개(결정됨, 2026-08-14)**
- 이름: `market-news-pipeline` (HLB 등 식별어 미포함).
- **공개**로 생성. 근거: 커밋되는 4개 JSON은 공개뉴스 파생물이고 시크릿은 코드에 없음(Actions Secrets). 발송세션이 raw를 **인증 없이** 읽어 구조가 가장 단순. 민감한 워치리스트는 `.gitignore`로 애초에 제외. 공개 리포는 Actions 분(minute)도 **무제한 무료**.

### 1-2. Secrets 등록 (Settings → Secrets and variables → Actions → New repository secret)
현재 PC의 `deal_ingest/config.json`에 있는 값을 그대로 복사한다(값을 이 문서에 옮겨적지 말 것):

| Secret 이름 | config.json 위치 |
|---|---|
| `NAVER_CLIENT_ID` | collectors.naver_news_api.client_id |
| `NAVER_CLIENT_SECRET` | collectors.naver_news_api.client_secret |
| `ECOS_API_KEY` | ecos.api_key |
| `KFP_SERVICE_KEY` | kfp.service_key |
| `FRED_API_KEY` | fred.api_key |

> 리포의 `config.json`은 이 5개 값이 **빈 문자열**로 커밋돼 있고, 실행 시 위 Secrets가 env로 주입되어 덮어쓴다(검증 완료).

### 1-3. seed(누적 상태) 이관
`fund_flow`·`kgf_execution`은 JSON 병합 누적이므로, 현재 PC의 최신 누적본을 리포에 넣어야 히스토리가 이어진다.
PC의 아래 2개 파일을 리포 `deal_ingest/`에 복사 후 커밋:
- `fund_flow_summary.json`
- `kgf_execution_summary.json`

(`articles.db`는 seed 불필요 — Actions cache로 런 간 유지되며 90일 창은 며칠이면 다시 채워짐. `recent_briefing.json`·`rate_summary.json`은 첫 실행이 새로 생성.)

### 1-4. push & 첫 수동 실행(검증)
```bash
# (리포 클론 후) 이 패키지 내용을 리포 루트에 복사하고:
git add .
git commit -m "init: deal_ingest unattended pipeline"
git push
```
- GitHub → Actions 탭 → **deal_ingest collect** → **Run workflow**(workflow_dispatch)로 즉시 1회 실행.
- **첫 실행의 목적 = 데이터센터 IP 수집 실증**(이 클라우드 샌드박스는 egress 차단이라 여기서 미검증). 
- 실행 후 **"Collector health summary"** 로그에서 소스별 `fetched=`/`failed`를 확인:
  - 네이버 API·FRED·ECOS·공공데이터(KFP)는 인증키 기반이라 정상 예상.
  - HTML 스크래핑(더벨·딜인사이트·인베스트조선·마켓인사이트·인포맥스)은 **일부 매체가 데이터센터 IP를 차단할 수 있음** → 차단 매체는 로그에 `failed`/`fetched=0`로 드러남. 이때 대응은 Phase 2 리스크 참조.

---

## Phase 2 — 병행운영 + 검증 (핵심: 기존 발송 유지)

1. **기존 PC 파이프라인·발송은 그대로 둔다.** 신규 Actions는 매일 병행 수집·커밋만 한다(발송 안 함).
2. 매 영업일, `verify_parallel.py`(동봉)로 **PC 산출물 vs 리포 산출물**을 비교:
   - 소스별·카테고리별 건수, `hlb_signal` 건수, rate_summary 항목 수.
   - 목표: 3~5영업일 연속으로 신규가 기존과 **동등 이상**(특히 HTML 매체 수집 건수, HLB 신호).
3. 발송 세션의 데이터 출처 전환은 **검증 통과 후**에만: 발송지침 B절 경로를 PC 로컬(`C:\...\deal_ingest\*.json`)에서 **GitHub raw URL**로 교체.
   - raw URL 형식: `https://raw.githubusercontent.com/<user>/<repo>/main/deal_ingest/recent_briefing.json` (rate_summary·fund_flow_summary·kgf_execution_summary 동일).
   - 0.1 수집완료 게이트: `last_run_status.json`도 raw로 읽어 `generated_at` KST 날짜 == 오늘 판정(동일 로직).

### Phase 2 리스크 & 대응
- **HTML 매체 차단(데이터센터 IP)**: 해당 매체만 `config.json`에서 `enabled:false` 처리하거나, 네이버 검색 API가 같은 기사를 이미 커버하는지 확인(딜·M&A는 네이버로 상당수 커버). 심하면 Actions에 프록시 추가는 비용 발생 → 그 매체는 발송세션 WebSearch 보완으로 대체.
- **구글시트 비공개**: `kgf_sheet_sync`가 CSV 대신 로그인 HTML 받으면 aggregate가 상수 fallback → 시트를 "링크가 있는 모든 사용자=뷰어"로 유지.
- **커밋 충돌**: 동시 실행 방지(concurrency), 매 런 push. 실패 시 다음 런이 이어받음.

---

## Phase 3 — 컷오버 & PC 작업 폐기 (검증 완료 확인 후)

체크리스트(모두 충족 시에만 폐기):
- [ ] Actions 수집이 5영업일 연속 성공(health `status: ok`, 3개 산출물 valid).
- [ ] 신규 산출물 기준 발송이 **정상 발송 확인**(멱등성·게이트·링크 포맷 이상 없음).
- [ ] `verify_parallel` 동등성 통과.

폐기 절차:
1. PC: Windows Task Scheduler의 `deal_ingest` 06:00 작업 **비활성화**(삭제 아닌 disable 먼저).
2. 발송지침: 데이터 출처가 raw로 완전 전환됐는지 최종 확인, device bridge/Chrome MCP 의존 문구 제거.
3. 1주 무사고 후 PC Task 삭제·`register_task.ps1`/`run_orchestrator.ps1` 아카이브.

**롤백**: 문제 발생 시 발송지침 데이터 출처를 PC 경로로 되돌리고 PC Task 재활성화(disable만 했으므로 즉시 복구).

---

## 부록 — 이 패키지 변경점(원본 대비)
- `orchestrator.py`: `load_config`에 `_overlay_secrets_from_env()` 추가 — env가 config.json 빈값을 덮어씀(로컬 실행 하위호환 유지).
- `config.json`: 시크릿 5종 빈 문자열 + naver 쿼리 5→**30종**(HLB 전 계열·고임팩트 이벤트, 직전 작업 반영).
- `collectors/core.py`·`keywords.json`: `hlb_signal` 태깅(직전 작업 반영).
- `.github/workflows/collect.yml`: cron·시크릿·cache·커밋·health 진단.
- `.gitignore`: DB는 cache(비커밋), 산출물 JSON 커밋, 워치리스트 제외.
- 그 외 수집기·rate·fund_flow·kgf 로직은 **원본 그대로**(무결성 유지).
