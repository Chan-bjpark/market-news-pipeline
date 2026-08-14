<#
  deal_ingest 무인화 — 원클릭 배포 (Windows PowerShell)

  전제(최초 1회):
    1) Git 설치
    2) GitHub CLI 설치:  winget install --id GitHub.cli
    3) 로그인:           gh auth login      (GitHub 계정 인증)

  사용:
    # 이 패키지(zip)를 푼 폴더에서 실행. 시크릿·seed는 기존 PC의 deal_ingest에서 자동으로 읽음.
    powershell -ExecutionPolicy Bypass -File .\bootstrap_deploy.ps1

  동작:
    - public 리포 생성 → push
    - 기존 config.json의 실 키 5종을 GitHub Secrets로 등록(값은 화면에 출력 안 함)
    - fund_flow/kgf 누적 JSON을 seed로 복사
    - 첫 수집 1회 수동 실행(workflow_dispatch)
    - 발송 세션이 읽을 raw URL 출력
#>

param(
  [string]$RepoName   = "market-news-pipeline",
  [string]$Visibility = "public",
  # 기존 PC 파이프라인 경로(시크릿·seed 원본)
  [string]$SourceDir  = "C:\Users\lg\Documents\Claude\Projects\뉴스 정기발송\deal_ingest",
  # 이 패키지를 푼 위치(리포 루트). 기본=현재 폴더
  [string]$PkgDir     = "."
)

$ErrorActionPreference = "Stop"
function Ok($m){Write-Host "  [OK] $m" -ForegroundColor Green}
function Info($m){Write-Host "==> $m" -ForegroundColor Cyan}
function Die($m){Write-Host "  [ERROR] $m" -ForegroundColor Red; exit 1}

# 0. 전제 확인
Info "전제 확인 (git, gh, 인증)"
if(-not (Get-Command git -ErrorAction SilentlyContinue)){ Die "git 미설치" }
if(-not (Get-Command gh  -ErrorAction SilentlyContinue)){ Die "GitHub CLI(gh) 미설치 → winget install --id GitHub.cli" }
gh auth status 2>$null; if($LASTEXITCODE -ne 0){ Die "gh 미인증 → gh auth login 먼저 실행" }
Ok "전제 충족"

# 1. 시크릿 원본 로드(기존 config.json)
$srcCfg = Join-Path $SourceDir "config.json"
if(-not (Test-Path $srcCfg)){ Die "기존 config.json 없음: $srcCfg" }
$cfg = Get-Content $srcCfg -Raw -Encoding UTF8 | ConvertFrom-Json
$secrets = @{
  NAVER_CLIENT_ID     = $cfg.collectors.naver_news_api.client_id
  NAVER_CLIENT_SECRET = $cfg.collectors.naver_news_api.client_secret
  ECOS_API_KEY        = $cfg.ecos.api_key
  KFP_SERVICE_KEY     = $cfg.kfp.service_key
  FRED_API_KEY        = $cfg.fred.api_key
}
foreach($k in $secrets.Keys){ if([string]::IsNullOrWhiteSpace($secrets[$k])){ Die "시크릿 비어있음: $k (기존 config.json 확인)" } }
Ok "시크릿 5종 로드(값 비출력)"

# 2. seed 누적 JSON 복사
Info "seed(누적 상태) 복사"
foreach($f in @("fund_flow_summary.json","kgf_execution_summary.json")){
  $s = Join-Path $SourceDir $f
  $d = Join-Path $PkgDir  "deal_ingest\$f"
  if(Test-Path $s){ Copy-Item $s $d -Force; Ok "seed: $f" } else { Write-Host "  [warn] seed 없음(신규 생성됨): $f" -ForegroundColor Yellow }
}

# 3. git init + 커밋 + public 리포 생성/push
Info "리포 생성 및 push ($Visibility)"
Set-Location $PkgDir
if(-not (Test-Path ".git")){ git init -b main | Out-Null }
# Git 신원(최초 PC에서 커밋하려면 필수) — GitHub 로그인 계정으로 설정
$me = (gh api user --jq .login)
if([string]::IsNullOrWhiteSpace($me)){ Die "GitHub 사용자 조회 실패 — gh auth login 재확인" }
git config user.name  $me
git config user.email "$me@users.noreply.github.com"
git add -A
if(git status --porcelain){ git commit -m "init: deal_ingest unattended pipeline" | Out-Null; Ok "커밋 생성" }
else { Ok "커밋할 변경 없음(이미 커밋됨)" }
# 이미 존재하면 스킵
$vis = "--$Visibility"
gh repo create $RepoName $vis --source=. --remote=origin --push
if($LASTEXITCODE -ne 0){ Die "gh repo create 실패(같은 이름 리포가 이미 있으면 -RepoName 으로 다른 이름 지정)" }
Ok "리포 생성·push 완료"

$full = "$me/$RepoName"

# 4. 시크릿 등록
Info "GitHub Secrets 등록"
foreach($k in $secrets.Keys){
  $secrets[$k] | gh secret set $k --repo $full | Out-Null
  Ok "secret: $k"
}

# 5. 첫 수집 수동 실행 (실패해도 치명적 아님 — 웹에서 수동 실행 가능)
Info "첫 수집 실행(workflow_dispatch) — 데이터센터 IP 수집 실증"
Start-Sleep -Seconds 5    # 워크플로 등록에 잠깐 여유
gh workflow run "collect.yml" --repo $full 2>$null
if($LASTEXITCODE -eq 0){ Ok "실행 트리거됨 (Actions 탭에서 로그 확인)" }
else { Write-Host "  [참고] 자동 실행이 아직 안 됨(등록 지연). GitHub Actions 탭에서 'collect' → 'Run workflow'를 직접 눌러주세요." -ForegroundColor Yellow }

# 6. 안내
$rawBase = "https://raw.githubusercontent.com/$full/main/deal_ingest"
Write-Host ""
Write-Host "================ 완료 ================" -ForegroundColor Green
Write-Host "리포:        https://github.com/$full"
Write-Host "Actions:     https://github.com/$full/actions"
Write-Host ""
Write-Host "발송 세션이 읽을 raw URL(검증 통과 후 발송지침에 반영):" -ForegroundColor Yellow
Write-Host "  $rawBase/recent_briefing.json"
Write-Host "  $rawBase/rate_summary.json"
Write-Host "  $rawBase/fund_flow_summary.json"
Write-Host "  $rawBase/kgf_execution_summary.json"
Write-Host "  $rawBase/logs/last_run_status.json"
Write-Host ""
Write-Host "다음: 첫 실행 로그의 'Collector health summary'에서 소스별 수집결과 확인 후" -ForegroundColor Cyan
Write-Host "      Claude에게 로그를 전달하면 Phase 2(병행검증·발송 출처 전환)를 이어서 진행합니다."
