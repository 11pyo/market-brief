<#
.SYNOPSIS
    MarketBrief 장중 시세 갱신(tick)을 Windows 작업 스케줄러에 등록합니다.
    주의 1: -LogonType Interactive 이므로 **해당 사용자가 로그온해 있을 때만** 실행됩니다.
            (install_task.ps1 과 같은 이유입니다. 다만 tick 은 LLM 을 호출하지 않으므로
            claude 인증과는 무관하고, Cloudflare 배포 자격증명만 필요합니다.)
    주의 2: 트리거 시각은 **이 PC의 로컬 시간** 기준입니다. PC 시간대가 KST 여야 의도대로 동작합니다.

.DESCRIPTION
    이 사이트는 정적 파일이라 차트가 실시간이 아니라 '파이프라인이 돌 때의 스냅샷'입니다.
    미국 지수는 07시 브리핑 시점에 장이 끝나 완결된 데이터지만, 한국 낮에 열려 있는
    코스피·원달러·비트코인은 스냅샷이 몇 시간씩 낡습니다. tick 이 그 간극을 메웁니다.

    scripts\run_tick.cmd 를 지정 간격(기본 30분)으로 반복 실행합니다.
    - 브리핑 본문은 건드리지 않습니다 (LLM 호출 없음, 쿼터 소모 없음)
    - 장중 시간대 판정은 앱이 합니다 (.env 의 INTRADAY_WINDOW, 기본 09:00-15:30 평일)
      따라서 장 시간 밖에 실행돼도 즉시 종료하며 네트워크를 쓰지 않습니다
    - 시세가 실제로 바뀐 경우에만 빌드·배포합니다 (휴장일에는 배포가 일어나지 않습니다)

    배포 횟수 참고: 30분 간격 · 6.5시간 · 월 22거래일 = 월 약 290회.
    일일 브리핑을 더해도 Cloudflare Pages 무료 한도(월 500회) 안에 들어옵니다.
    간격을 15분으로 줄이면 한도를 넘길 수 있으니 주의하십시오.

.PARAMETER IntervalMinutes
    반복 간격(분). 기본값 30. 허용 범위 10~120.

.PARAMETER StartTime
    반복 시작 시각 (HH:mm). 기본값 09:00

.PARAMETER DurationHours
    반복 지속 시간(시간). 기본값 7 (09:00 시작 시 16:00 까지 커버)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_tick_task.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install_tick_task.ps1 -IntervalMinutes 60

.NOTES
    Windows PowerShell 5.1 호환 (파이프라인 체인 연산자 && / 삼항 연산자 미사용)
    해제: powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_tick_task.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(10, 120)]                               # [SECURE] 범위 제한 - 과도한 배포/한도 초과 방지
    [int]$IntervalMinutes = 30,

    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]   # [SECURE] 시각 형식 화이트리스트 - 인자 주입 방지
    [string]$StartTime = "09:00",

    [ValidateRange(1, 12)]
    [int]$DurationHours = 7
)

$ErrorActionPreference = 'Stop'

$TaskName = 'MarketBrief Tick'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot 'run_tick.cmd'

if (-not (Test-Path -LiteralPath $RunScript)) {
    Write-Error "실행 스크립트를 찾을 수 없습니다: $RunScript"
    exit 1
}

Write-Host "작업 이름  : $TaskName"
Write-Host "실행 스크립트: $RunScript"
Write-Host "작업 폴더  : $ProjectRoot"
Write-Host "반복       : $StartTime 부터 $DurationHours 시간 동안 $IntervalMinutes 분 간격"
Write-Host ""

try {
    $action = New-ScheduledTaskAction -Execute $RunScript -WorkingDirectory $ProjectRoot

    # 매일 StartTime 에 시작해, DurationHours 동안 IntervalMinutes 간격으로 반복
    $trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Hours $DurationHours)).Repetition

    # tick 은 짧게 끝난다. 놓친 실행을 몰아서 돌릴 이유가 없으므로 StartWhenAvailable 을 켜지 않는다.
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'MarketBrief 장중 시세 갱신 (LLM 호출 없음, 변경 시에만 배포)' `
        -Force | Out-Null

    Write-Host "등록 완료: $TaskName" -ForegroundColor Green
}
catch {
    Write-Error "작업 등록 실패: $($_.Exception.Message)"
    exit 1
}

try {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "다음 실행 : $($info.NextRunTime)"
}
catch {
    Write-Host "다음 실행 시각을 조회하지 못했습니다 (등록 자체는 완료)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "확인      : schtasks /Query /TN `"$TaskName`""
Write-Host "즉시 실행 : schtasks /Run /TN `"$TaskName`""
Write-Host "해제      : powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_tick_task.ps1"

# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: 시각·간격 인자를 정규식/범위로 검증한 뒤에만 사용
#   - Improper Authorization: RunLevel Limited (관리자 권한 불필요), 비밀번호를 저장하지 않는 Interactive 로그온
#   - Infinite Loop: ExecutionTimeLimit 20분, MultipleInstances IgnoreNew 로 중복 실행 차단
#   - Missing Error Handling: 등록/조회 실패를 각각 처리하고 종료 코드로 전파
# Not Applied:
#   - [WARN] 사용자가 로그오프하면 실행되지 않는다 (Interactive 로그온의 의도된 제약).
#   - [WARN] 간격을 과도하게 줄이면 Cloudflare Pages 무료 배포 한도(월 500회)를 넘길 수 있다.
#     기본 30분은 월 약 290회로 한도 안이다.
# --------------------------------------------------
