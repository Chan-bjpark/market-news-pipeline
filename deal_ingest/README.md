# deal_ingest — 자본시장 뉴스 수집 파이프라인

매일 새벽 한국 자본시장 4대 매체(더벨·딜인사이트·마켓인사이트·인베스트조선)와 외부 뉴스 API에서 M&A·PE·펀드·기관출자 관련 기사를 자동 수집하여 SQLite DB에 적재한다. 매일 07:00 KST 발송되는 CEO 뉴스 브리핑 task가 이 DB를 조회하여 5~20건을 선별·발송한다.

## 운영 모델

```
06:00 KST  Windows Task Scheduler 트리거
            ↓
        orchestrator.py 실행
            ↓
        ┌─ thebell_rss        ─┐
        ├─ dealsite_html       │
        ├─ marketinsight_rss   ├→ articles.db (SQLite)
        ├─ investchosun_html   │
        └─ naver_news_api      ─┘
            ↓
07:00 KST  Cowork 스케줄러 — daily-news-slack-dm task
            ↓
        query_for_briefing.py 호출하여 최근 3일 기사 조회
            ↓
        Slack #news_claud 발송
```

## 폴더 구조

```
deal_ingest/
├── README.md                       (본 문서)
├── requirements.txt                Python 의존성
├── config.json                     API 키·수집 매체 설정 (gitignore 권장)
├── orchestrator.py                 메인 진입점 — Windows Task Scheduler가 호출
├── collectors/
│   ├── base.py                     공통 추상 클래스
│   ├── thebell_rss.py              ✅ Phase 1
│   ├── dealsite_html.py            🔜 Phase 2
│   ├── marketinsight_rss.py        🔜 Phase 2
│   ├── investchosun_html.py        🔜 Phase 2
│   └── naver_news_api.py           🔜 Phase 2
├── store/
│   ├── schema.sql                  SQLite 스키마
│   ├── db.py                       DB helper
│   └── articles.db                 생성됨 (gitignore)
├── query/
│   └── query_for_briefing.py       뉴스 task가 호출할 조회 인터페이스
├── logs/                           일자별 실행 로그
└── docs/
    ├── setup_guide.md              ⭐ Python 설치 + Task Scheduler 등록 가이드
    └── api_keys.md                 외부 API 키 발급 안내
```

## 셋업

→ `docs/setup_guide.md` 참조. 한 번만 진행하면 됨.

## 운영 확인

매일 아침 08:00 KST 이후 다음을 통해 어제 수집 상태 확인 가능:

```powershell
cd "C:\Users\lg\Documents\Claude\Projects\뉴스 정기발송\deal_ingest"
python query\query_for_briefing.py --status
```

## Phase 진행 상황

- **Phase 1 (현재)**: 더벨 RSS만 수집 → 동작 검증
- **Phase 2**: 딜인사이트·마켓인사이트·인베스트조선 추가 + 네이버 검색 API 보강
- **Phase 3**: 글로벌 매체(Reuters·Bloomberg·STAT 등) RSS 추가 검토

## 데이터 비날조 원칙

- 수집기는 매체가 실제로 발행한 기사만 저장한다. 추론·합성 금지.
- 매체명·발행일자·URL은 원본 그대로 보존한다.
- 페이월 등으로 본문 접근이 불가한 경우 `paywalled=true` 플래그만 표시하고 본문은 비워둔다(임의 요약 금지).
