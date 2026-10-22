# AI会話観察室：デスクトップに起動・停止のショートカットを作る（最初に1回だけ実行）
# デスクトップの場所は Windows に問い合わせる（OneDrive などに移動していても正しい場所になる）
#   -Yes         … 確認せずに作る
#   -Destination … 作る場所を指定する（動作確認用。省略するとデスクトップ）
param([switch]$Yes, [string]$Destination)

. (Join-Path $PSScriptRoot 'common.ps1')

$desktop = if ($Destination) { $Destination } else { [Environment]::GetFolderPath('Desktop') }
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$items = @(
    @{ Name = 'AI会話観察室 起動'; Script = 'start.ps1'; Icon = 'shell32.dll,137'; Desc = 'AI会話観察室を起動してブラウザで開きます' },
    @{ Name = 'AI会話観察室 停止'; Script = 'stop.ps1';  Icon = 'shell32.dll,27';  Desc = 'AI会話観察室のサーバーを停止します' }
)

Write-Host "ショートカットを作る場所: $desktop"
foreach ($i in $items) { Write-Host "  ・$($i.Name).lnk" }
if (-not $Yes) { Read-Host 'Enter で作成／このウィンドウを閉じると中止' | Out-Null }

$shell = New-Object -ComObject WScript.Shell
foreach ($i in $items) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$($i.Name).lnk"))
    $lnk.TargetPath = $ps
    # 実行ポリシーの設定は変えず、この実行のときだけ Bypass で動かす
    $lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot $i.Script)
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = Join-Path $env:SystemRoot "System32\$($i.Icon)"
    $lnk.Description = $i.Desc
    $lnk.Save()
    Write-Host "作成しました: $(Join-Path $desktop "$($i.Name).lnk")"
}
