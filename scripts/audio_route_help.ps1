# 打开 Windows 11 的「应用音量和设备首选项」，并按双 QQ 场景打印需要设置的映射。
# 仅打开设置页与打印说明，不修改任何系统设置。
$ErrorActionPreference = 'Stop'

Write-Host '需要设置的两个 QQ 客户端（按 exe 路径区分）：'
Write-Host '  A 主号 : C:\qq-bot\napcat\QQ.exe                       输入=实体麦克风   输出=耳机'
Write-Host '  B 小号 : C:\Program Files\Tencent\QQNT\QQ.exe          输入=CABLE Output  输出=扬声器 (Senary Audio)'
Write-Host ''
Write-Host '如果「应用音量和设备首选项」里只看到一个 QQ，请先把两个客户端都打开再进设置刷新。'
Write-Host ''
Write-Host '打开设置页…'
Start-Process 'ms-settings:apps-volume'
