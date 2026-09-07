<#
.SYNOPSIS
    MarketBrief 일일 브리핑을 Windows 작업 스케줄러에 등록합니다.
    주의 1: -LogonType Interactive 이므로 **해당 사용자가 로그온해 있을 때만** 실행됩니다.
            claude CLI가 구독 OAuth 자격증명을 keychain(자격증명 관리자)에서 읽어야 하므로
            의도된 제약입니다. 로그오프 상태에서 실행하려면 비밀번호 저장(Password 로그온)이
            필요하고, 그러면 keychain 접근이 막혀 인증이 깨집니다.
    주의 2: 트리거 시각(-Time, 기본 07:00)은 **이 PC의 로컬 시간** 기준입니다.
            PC 시간대가 KST일 때 07:00 KST가 됩니다.

.DESCRIPTION
    현재 사용자 계정으로 매일 지정 시각에 scripts\run_daily.cmd 를 실행하는 작업을 만듭니다.
    대화형 로그온(Interactive) 방식이므로 비밀번호를 저장하지 않습니다.
    -StartWhenAvailable  : PC가 꺼져 있어 놓친 작업을 다음 부팅 후 실행
    -WakeToRun           : 절전 상태면 깨워서 실행
    -ExecutionTimeLimit  : 2시간 (LLM 생성 + 다국어 번역 + 빌드/배포 전체 여유)
    이미 같은 이름의 작업이 있으면 -Force 로 덮어씁니다.

.PARAMETER Time
    실행 시각 (HH:mm). 기본값 07:00

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -Time "06:30"

.NOTES
    Windows PowerShell 5.1 호환 (파이프라인 체인 연산자 && / 삼항 연산자 미사용)
#>
[CmdletBinding()]
param(
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]   # [SECURE] 시각 형식 화이트리스트 - 인자 주입 방지
    [string]$Time = "07:00"
)

$ErrorActionPreference = 'Stop'

$TaskName = 'MarketBrief Daily'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot 'run_daily.cmd'

if (-not (Test-Path -LiteralPath $RunScript)) {
    Write-Error "실행 스크립트를 찾을 수 없습니다: $RunScript"
    exit 1
}

Write-Host "작업 이름  : $TaskName"
Write-Host "실행 파일  : $RunScript"
Write-Host "작업 폴더  : $ProjectRoot"
Write-Host "실행 시각  : 매일 $Time"
Write-Host ""

try {
    # [SECURE] 실행 대상을 스크립트 절대 경로로 고정 - 임의 명령 실행 방지
    $action = New-ScheduledTaskAction -Execute $RunScript -WorkingDirectory $ProjectRoot

    $trigger = New-ScheduledTaskTrigger -Daily -At $Time

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries

    # [SECURE] Interactive 로그온 타입 - 비밀번호를 저장하지 않음 (Hard-coded Credentials 방지).
    # 대가로 사용자가 로그온해 있을 때만 실행된다 (claude CLI의 keychain OAuth 접근에 필요).
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
        -Description 'AI 모닝 마켓 브리핑 생성 및 정적 사이트 배포 (mbrief daily)' `
        -Force | Out-Null

    Write-Host "등록 완료: $TaskName" -ForegroundColor Green
}
catch {
    # [SECURE] 상세 오류는 콘솔에만, 자격증명은 포함되지 않음 (Error Message Information Exposure)
    Write-Error "작업 등록에 실패했습니다: $($_.Exception.Message)"
    exit 1
}

try {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ""
    Write-Host "다음 실행 예정 : $($info.NextRunTime)"
    Write-Host "마지막 실행    : $($info.LastRunTime)"
}
catch {
    Write-Warning "다음 실행 시각을 조회하지 못했습니다: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "확인 : schtasks /Query /TN `"$TaskName`""
Write-Host "즉시 실행 : schtasks /Run /TN `"$TaskName`""
Write-Host "해제 : powershell -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'uninstall_task.ps1')`""
exit 0

# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: -Time 파라미터를 정규식으로 검증, 실행 대상은 절대 경로 상수
#   - Hard-coded Credentials: Interactive 로그온 타입으로 비밀번호 저장 없음
#   - Incorrect Permission on Critical Resource: RunLevel Limited(관리자 권한 없이 실행)
#   - Missing Error Handling: try/catch로 등록 실패를 처리하고 0이 아닌 종료 코드 반환
#   - Improper Exception Handling: 빈 catch 없음, 모든 실패 경로에서 메시지 출력
#   - Infinite Loop: ExecutionTimeLimit 2시간, MultipleInstances IgnoreNew로 중복 실행 방지
# Not Applied:
#   - [WARN] Interactive 로그온이라 사용자가 로그오프 상태면 작업이 실행되지 않는다.
#     비밀번호 저장 방식으로 바꾸면 무인 실행은 되지만 keychain OAuth가 깨진다 (의도된 절충).
#   - [WARN] Code Download Without Integrity Check: run_daily.cmd의 무결성을 검증하지 않는다.
#     프로젝트 폴더 쓰기 권한을 가진 사용자는 실행 내용을 바꿀 수 있으므로 폴더 ACL을 확인할 것.
# --------------------------------------------------
