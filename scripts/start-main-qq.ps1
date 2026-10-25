# 用「另一套安装」的 QQ 启动主号客户端（仅启动，不做任何注入）。
# 机器人使用的是 C:\Program Files\Tencent\QQNT\QQ.exe，这里故意用不同路径，
# 以便两个客户端共存；若发现机器人掉线，请立即关掉这个客户端。
param(
    [string]$QQExe = 'C:\qq-bot\napcat\QQ.exe'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $QQExe)) {
    Write-Host "找不到第二套 QQ: $QQExe" -ForegroundColor Red
    Write-Host '请确认 NapCat 自带客户端已解压（安装器 15_napcat_tools.ps1 下载的那套）。'
    exit 1
}
$running = Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" |
    Where-Object { $_.ExecutablePath -eq $QQExe }
if ($running) {
    Write-Host "主号客户端已在运行（pid=$($running.ProcessId)）"
    exit 0
}
Write-Host "启动主号客户端: $QQExe"
Start-Process -FilePath $QQExe
Write-Host '登录后检查：机器人是否仍在线（AstrBot 6199 连接 / logs\llbot.log 无掉线）。'
