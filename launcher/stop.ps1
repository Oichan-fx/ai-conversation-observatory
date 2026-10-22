# AI会話観察室：停止スクリプト
# このアプリのサーバー（仮想環境の親プロセスと、本体の子プロセス）だけを止める。
# 同じポートを使う別のアプリは止めない。
#   -Yes     … 確認せずに止める（動作確認用）
#   -NoPause … 最後の Enter 待ちをしない（動作確認用）
param([switch]$Yes, [switch]$NoPause)

. (Join-Path $PSScriptRoot 'common.ps1')
$Host.UI.RawUI.WindowTitle = 'AI会話観察室 停止'

function Finish([string]$Message, [string]$Color = 'White', [int]$Code = 0) {
    Write-Host ''
    Write-Host $Message -ForegroundColor $Color
    Write-LauncherLog "停止: $Message"
    if ($NoPause) { exit $Code }
    if ($Code -eq 0) {
        Write-Host 'このウィンドウは自動で閉じます。'
        Start-Sleep -Seconds 3
    } else {
        Wait-Enter 'Enter キーで閉じます'
    }
    exit $Code
}

try {
    $state = Get-ServerState

    if ($state.State -eq 'other') {
        $o = $state.Owner
        Finish ("ポート $Port は別のアプリ（$($o.Name)、プロセス番号 $($o.ProcessId)）が使っています。" +
                "このアプリのサーバーではないので、止めませんでした。") 'Yellow' 1
    }
    if ($state.Processes.Count -eq 0) {
        Finish 'AI会話観察室のサーバーは起動していません。' 'White' 0
    }

    $ids = ($state.Processes | ForEach-Object { $_.ProcessId }) -join '、'
    Write-Host "AI会話観察室のサーバーが動いています（プロセス番号 $ids）。"
    Write-Host ''

    # 進行中の会話（DBを読み取るだけ。画面・API・Gemini API は使わない）
    $status = @()
    if (Test-Path $Python) {
        $env:PYTHONIOENCODING = 'utf-8'
        $status = @(& $Python -I (Join-Path $PSScriptRoot 'conversation_status.py') 2>$null)
    }
    if ($status.Count -gt 0) {
        Write-Host '進行中の会話があります:' -ForegroundColor Yellow
        $status | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    } else {
        Write-Host '進行中の会話はありません。'
    }

    if (-not $Yes) {
        Write-Host ''
        Write-Host '停止しますか？会話中の場合、その会話は「中断」になります。'
        Read-Host 'Enter で停止／このウィンドウを閉じると中止' | Out-Null
    }

    # 本体（子）を先に、仮想環境の入口（親）を後に止める
    $ordered = @($state.Processes | Sort-Object { if ($_.ExecutablePath -and (Test-ContainsPath $_.ExecutablePath '\.venv\')) { 1 } else { 0 } })
    foreach ($p in $ordered) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
    }

    # 止まったことを確かめる（最大10秒）
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 300
        $left = @($state.Processes | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
        $owner = Get-PortOwner
        $portBusy = $owner -and ($state.Processes.ProcessId -contains $owner.ProcessId)
    } while (($left.Count -gt 0 -or $portBusy) -and (Get-Date) -lt $deadline)

    if ($left.Count -gt 0) {
        Finish ("止まらないプロセスがあります（プロセス番号 " + (($left | ForEach-Object { $_.ProcessId }) -join '、') +
                "）。タスクマネージャーで確認してください。") 'Red' 1
    }
    if (Test-Path $PidFile) { Remove-Item -Force $PidFile -ErrorAction SilentlyContinue }
    $msg = 'AI会話観察室のサーバーを停止しました。'
    if ($status.Count -gt 0) { $msg += '進行中だった会話は、次に起動したときに「中断」として記録されます。' }
    Finish $msg 'Green' 0
}
catch {
    Finish "予期しないエラーが発生しました: $($_.Exception.Message)" 'Red' 1
}
