# AI会話観察室：起動スクリプト
# サーバーを裏で起動し、応答を確かめてから既定のブラウザで開く。
# すでに起動していれば、二重に起動せずブラウザを開くだけにする。
#   -NoBrowser … ブラウザを開かない（動作確認用）
#   -NoPause   … 失敗しても Enter 待ちをしない（動作確認用）
param([switch]$NoBrowser, [switch]$NoPause)

. (Join-Path $PSScriptRoot 'common.ps1')
$Host.UI.RawUI.WindowTitle = 'AI会話観察室 起動'
$TimeoutSec = 30
$ServerLog  = Join-Path $LogDir 'server.log'
$ServerErr  = Join-Path $LogDir 'server.err.log'

function Show-Tail([string]$Path, [int]$Lines = 30) {
    if (Test-Path $Path) {
        $text = Get-Content -Path $Path -Encoding UTF8 -Tail $Lines -ErrorAction SilentlyContinue
        if ($text) {
            Write-Host ''
            Write-Host "---- $(Split-Path -Leaf $Path) の最後の部分 ----" -ForegroundColor DarkGray
            $text | ForEach-Object { Write-Host $_ }
        }
    }
}

function Fail([string]$Message, [string[]]$Hints = @(), [switch]$WithLogs) {
    Write-Host ''
    Write-Host '起動できませんでした。' -ForegroundColor Red
    Write-Host $Message -ForegroundColor Red
    foreach ($h in $Hints) { Write-Host "  ・$h" -ForegroundColor Yellow }
    Write-LauncherLog "起動失敗: $Message"
    if ($WithLogs) {   # 今回起動したサーバーのログだけを見せる（前回のログで混乱しないように）
        Show-Tail $ServerErr
        Show-Tail $ServerLog 10
    }
    Write-Host ''
    Write-Host "記録の場所: $LogDir" -ForegroundColor DarkGray
    if (-not $NoPause) { Wait-Enter 'Enter キーで閉じます' }
    exit 1
}

function Open-Browser {
    if ($NoBrowser) { Write-Host "（確認用のため、ブラウザは開きません: $Url）"; return '（確認用のためブラウザは開いていません）' }
    Start-Process $Url
    return "ブラウザで $Url を開きました。"
}

function Exit-Success([string]$Message) {
    Write-Host $Message -ForegroundColor Green
    Write-LauncherLog $Message
    Write-Host 'このウィンドウは自動で閉じます。'
    Start-Sleep -Seconds 3
    exit 0
}

Write-Host 'AI会話観察室を起動しています…'

# 起動の手続きは同時に1つだけ（ダブルクリックが続いたとき用）
$mutex = New-Object System.Threading.Mutex($false, 'Local\AIKaiwaKansatsushitsu_Launcher')
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Host '別の起動処理が進んでいます。終わるのを待っています…'
        try { $owned = $mutex.WaitOne(($TimeoutSec + 10) * 1000) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { Fail '別の起動処理が終わりませんでした。しばらく待ってからもう一度お試しください。' }
    }

    # 1. 仮想環境の Python があるか
    if (-not (Test-Path $Python)) {
        Fail "仮想環境の Python が見つかりません: $Python" @(
            'README の「2. セットアップ」（仮想環境の作成とライブラリのインストール）を行ってください。')
    }

    # 2. すでに起動しているか／別のアプリがポートを使っていないか
    $state = Get-ServerState
    if ($state.State -eq 'ours') {
        $opened = Open-Browser
        Exit-Success "すでに起動していました（二重には起動していません）。$opened"
    }
    if ($state.State -eq 'other') {
        $o = $state.Owner
        Fail "ポート $Port を別のアプリが使っています（$($o.Name)、プロセス番号 $($o.ProcessId)）。" @(
            'そのアプリを終了してから、もう一度起動してください。',
            '停止用のショートカットは、このアプリ以外は止めません。')
    }
    if ($state.Processes.Count -gt 0) {
        # ポートは空いているのに app.py が動いている＝起動途中か、止まりかけ
        Write-Host '前回のサーバーがまだ残っているようです。応答を待ちます…'
    } else {
        # 3. サーバーを裏で起動（ログは logs フォルダへ。前回分は1世代だけ残す）
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
        foreach ($f in @($ServerLog, $ServerErr)) {
            if (Test-Path $f) { Move-Item -Force $f ($f -replace '\.log$', '.prev.log') }
        }
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONUNBUFFERED = '1'
        try {
            $proc = Start-Process -FilePath $Python -ArgumentList ('"{0}"' -f $AppPy) -WorkingDirectory $Root `
                -WindowStyle Hidden -RedirectStandardOutput $ServerLog -RedirectStandardError $ServerErr -PassThru
        } catch {
            Fail "サーバーを起動できませんでした: $($_.Exception.Message)"
        }
        $null = $proc.Handle   # 終了コードを後で読めるように、ハンドルを先に取っておく（PowerShell 5.1 の仕様）
        Set-Content -Path $PidFile -Value $proc.Id -Encoding ASCII
        Write-LauncherLog "サーバーを起動しました（プロセス番号 $($proc.Id)）"
    }

    # 4. 応答が返るまで待つ（このアプリ自身への問い合わせ。Gemini API は呼ばない）
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($proc -and $proc.HasExited) {
            Fail "サーバーがすぐに終了しました（終了コード $($proc.ExitCode)）。下のログに原因が出ています。" @(
                'ライブラリが足りない場合は、README の「2-3. ライブラリのインストール」を行ってください。') -WithLogs
        }
        $s = Get-ServerState
        if ($s.State -eq 'other') {
            Fail "ポート $Port を別のアプリが使い始めました（$($s.Owner.Name)、プロセス番号 $($s.Owner.ProcessId)）。"
        }
        if ($s.State -eq 'ours') {
            try {
                $r = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/current" -TimeoutSec 3
                if ($r.StatusCode -eq 200) {
                    $opened = Open-Browser
                    Exit-Success "起動しました。$opened"
                }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    Fail "$TimeoutSec 秒待ってもサーバーが応答しませんでした。" @(
        'もう一度起動してみてください。続く場合は下のログを確認してください。') -WithLogs
}
catch {
    Fail "予期しないエラーが発生しました: $($_.Exception.Message)" -WithLogs
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
