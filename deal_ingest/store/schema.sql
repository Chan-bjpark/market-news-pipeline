-- deal_ingest SQLite 스키마
-- 모든 매체 기사를 단일 articles 테이블에 적재. 매체 구분은 source 컬럼.

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,        -- 'thebell' | 'dealsite' | 'marketinsight' | 'investchosun' | 'naver_api' | ...
    source_label  TEXT NOT NULL,        -- 한글 매체명 ('더벨', '딜인사이트', '마켓인사이트', '인베스트조선', ...)
    url           TEXT NOT NULL UNIQUE, -- 정규화된 URL (트래킹 파라미터 제거됨). 중복 제거 키.
    title         TEXT NOT NULL,
    summary       TEXT,                 -- 매체 제공 요약 또는 첫 문단 (없으면 NULL)
    published_at  TEXT NOT NULL,        -- ISO 8601 with timezone (KST 우선, 없으면 UTC)
    fetched_at    TEXT NOT NULL,        -- ISO 8601 UTC — 수집 시점
    category      TEXT,                 -- 'M&A' | 'PE' | 'fund_raise' | 'lp_commit' | 'cap_market' | 'other'
    paywalled     INTEGER DEFAULT 0,    -- 0=공개, 1=페이월 (본문 접근 불가)
    raw_meta      TEXT                  -- JSON 문자열: 매체별 원본 메타 (저자, 태그, 카테고리 등)
);

CREATE INDEX IF NOT EXISTS idx_published_desc ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_source         ON articles(source);
CREATE INDEX IF NOT EXISTS idx_category       ON articles(category);

-- 실행 로그
CREATE TABLE IF NOT EXISTS run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started   TEXT NOT NULL,        -- ISO 8601 UTC
    run_finished  TEXT,                 -- ISO 8601 UTC
    collector     TEXT NOT NULL,        -- 수집기 이름
    status        TEXT NOT NULL,        -- 'success' | 'partial' | 'failed'
    fetched_count INTEGER DEFAULT 0,
    new_count     INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runlog_started ON run_log(run_started DESC);
