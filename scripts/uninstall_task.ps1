<#
.SYNOPSIS
    MarketBrief 일일 브리핑 작업을 Windows 작업 스케줄러에서 제거합니다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_task.ps1

.NOTES
    Windows PowerShell 5.1 호환 (파이프라인 체인 연산자 && / 삼항 연산자 미사용)
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$TaskName = 'MarketBrief Daily'

$existing = $null
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
catch {
    # 등록되어 있지 않은 것은 오류가 아니다.
    Write-Host "등록된 작업이 없습니다: $TaskName"
    exit 0
}

if ($null -eq $existing) {
    Write-Host "등록된 작업이 없습니다: $TaskName"
    exit 0
}

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "제거 완료: $TaskName" -ForegroundColor Green
}
catch {
    Write-Error "작업 제거에 실패했습니다: $($_.Exception.Message)"
    exit 1
}

exit 0

# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: 외부 인자를 받지 않고 고정 작업 이름 상수만 사용
#   - Missing Error Handling: 미등록 상태와 제거 실패를 구분해 처리, 실패 시 종료 코드 1
#   - Improper Exception Handling: 빈 catch 없음, 모든 경로에서 메시지 출력
# Not Applied:
#   - [WARN] 해당 없음 - 작업 제거 전용 스크립트, 자격증명·외부 입출력 없음
# --------------------------------------------------
