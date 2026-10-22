# AI会話観察室：起動・停止スクリプトの共通部分（start.ps1 / stop.ps1 から読み込む）
# パスはこのファイルの場所から組み立てる（フォルダ名を直接書かない）

$Root        = Split-Path -Parent $PSScriptRoot
$Port        = 8000
$Url         = "http://127.0.0.1:$Port"
$Python      = Join-Path $Root '.venv\Scripts\python.exe'
$AppPy       = Join-Path $Root 'app.py'
$LogDir      = Join-Path $Root 'logs'
$LauncherLog = Join-Path $LogDir 'launcher.log'
$PidFile     = Join-Path $LogDir 'server.pid'

# 画面とファイルの文字コードを UTF-8 にする（Python の出力を正しく読むため）
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

function Write-LauncherLog([string]$Message) {
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
        $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        [System.IO.File]::AppendAllText($LauncherLog, $line + "`r`n", (New-Object System.Text.UTF8Encoding($true)))
    } catch { }
}

function Test-ContainsPath([string]$Text, [string]$Path) {
    return $Text -and ($Text.IndexOf($Path, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Test-AppPage {
    # このアプリの画面かどうかを、トップページの題名で確かめる（Gemini API は呼ばない）
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "$Url/" -TimeoutSec 3
        return ($r.StatusCode -eq 200) -and ($r.Content -match '<title>AI会話観察室</title>')
    } catch { return $false }
}

function Get-PortOwner {
    # 8000 番で待ち受けているプロセス（なければ $null）
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
}

function Get-AppProcesses {
    # このフォルダの app.py を実行しているプロセス（仮想環境の親と、本体の子の両方）
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { Test-ContainsPath $_.CommandLine $AppPy })
}

function Get-ServerState {
    # State: none（8000番は空き）/ ours（このアプリ）/ other（別のアプリ）
    # Processes: 止める対象（このアプリのときだけ）
    $owner = Get-PortOwner
    $procs = Get-AppProcesses
    if (-not $owner) {
        return @{ State = 'none'; Owner = $null; Processes = $procs }
    }
    $isOurs = Test-ContainsPath $owner.CommandLine $AppPy
    if (-not $isOurs) {
        # 以前の手動起動（相対パスの app.py）にも対応：コマンドラインに app.py があり、画面の題名も一致するとき
        $isOurs = ($owner.CommandLine -match 'app\.py') -and (Test-AppPage)
        if ($isOurs) {
            $procs = @($owner)
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($owner.ParentProcessId)"
            if ($parent -and $parent.Name -eq 'python.exe' -and ($parent.CommandLine -match 'app\.py')) { $procs += $parent }
        }
    }
    if ($isOurs) {
        if ($procs.ProcessId -notcontains $owner.ProcessId) { $procs += $owner }
        return @{ State = 'ours'; Owner = $owner; Processes = $procs }
    }
    return @{ State = 'other'; Owner = $owner; Processes = @() }
}

function Wait-Enter([string]$Message) {
    Write-Host ''
    Read-Host $Message | Out-Null
}
