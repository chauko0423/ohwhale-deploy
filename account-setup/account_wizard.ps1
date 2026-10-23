param([string]$PreviewPath = '')
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'install/common.ps1')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
# PowerShell 5.1 otherwise converts Chinese JSON sent to Python into ASCII.
$OutputEncoding = New-Object Text.UTF8Encoding($false)
$Helper = Join-Path $PSScriptRoot 'account_form.py'
$script:WizardInitial = (& $ProjectPython $Helper read | Out-String) | ConvertFrom-Json
if (-not $script:WizardInitial.ok) {
    [Windows.Forms.MessageBox]::Show($script:WizardInitial.detail, '无法打开配置') | Out-Null
    exit 1
}
$Form = New-Object Windows.Forms.Form
$Form.Text = '哦鲸鲸 · 首次配置'
$Form.ClientSize = New-Object Drawing.Size(740, 650)
$Form.StartPosition = 'CenterScreen'
$Form.FormBorderStyle = 'FixedDialog'
$Form.MaximizeBox = $false
$Form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 10)
$Form.AutoScaleMode = 'Dpi'

function Add-Label([string]$Text, [int]$Y, [int]$Height = 28) {
    $Label = New-Object Windows.Forms.Label
    $Label.Text = $Text
    $Label.Location = New-Object Drawing.Point(28, $Y)
    $Label.Size = New-Object Drawing.Size(680, $Height)
    $Form.Controls.Add($Label)
    return $Label
}
$Title = Add-Label '填写 4 项，即可准备好账号配置' 20 36
$Title.Font = New-Object Drawing.Font('Microsoft YaHei UI', 16, [Drawing.FontStyle]::Bold)
$null = Add-Label '只保存在这台电脑。保存后会导入默认人设；QQ 登录和通话测试随后进行。' 61 40
$Boxes = @{}
$Fields = @(
    @('owner_qq', '1. 主人 QQ 号', '填写你平时与机器人聊天的 QQ 号，不是机器人登录的 QQ。'),
    @('DEEPSEEK_API_KEY', '2. DeepSeek API 密钥', '用于生成聊天回复。粘贴完整密钥；不是 DeepSeek 网页登录密码。'),
    @('DASHSCOPE_API_KEY', '3. 百炼 / DashScope API 密钥', '用于通话语音识别和画面理解。粘贴百炼控制台的完整 API Key。'),
    @('ASTRBOT_DASHBOARD_PASSWORD', '4. 设置本项目的面板密码', '自己新设一个：至少 8 位，包含大写、小写字母和数字；不是 QQ 密码。')
)
for ($i = 0; $i -lt $Fields.Count; $i++) {
    $Field = $Fields[$i]
    $Y = 108 + $i * 104
    $null = Add-Label $Field[1] $Y
    $Box = New-Object Windows.Forms.TextBox
    $Box.Location = New-Object Drawing.Point(28, ($Y + 29))
    $Box.Size = New-Object Drawing.Size(680, 30)
    $Box.Text = [string]$script:WizardInitial.($Field[0])
    $Box.UseSystemPasswordChar = ($i -gt 0)
    $Box.TabIndex = $i
    $Form.Controls.Add($Box)
    $Boxes[$Field[0]] = $Box
    $Note = Add-Label $Field[2] ($Y + 63) 34
    $Note.ForeColor = [Drawing.Color]::DimGray
}
$Show = New-Object Windows.Forms.CheckBox
$Show.Text = '显示密钥和密码（请留意旁人或屏幕共享）'
$Show.Location = New-Object Drawing.Point(28, 527)
$Show.Size = New-Object Drawing.Size(680, 28)
$Show.TabIndex = 4
$Show.Add_CheckedChanged({
    foreach ($Key in @('DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','ASTRBOT_DASHBOARD_PASSWORD')) {
        $Boxes[$Key].UseSystemPasswordChar = -not $Show.Checked
    }
})
$Form.Controls.Add($Show)
$Status = Add-Label '已经填写过的内容会自动带入。无需查找或编辑配置文件。' 561 34
$Save = New-Object Windows.Forms.Button
$Save.Text = '保存并完成配置'
$Save.Location = New-Object Drawing.Point(487, 604)
$Save.Size = New-Object Drawing.Size(221, 34)
$Save.TabIndex = 5
$Form.Controls.Add($Save)
$Form.AcceptButton = $Save
$Save.Add_Click({
    $Save.Enabled = $false
    $Form.UseWaitCursor = $true
    try {
        $Request = @{ revision = $script:WizardInitial.revision }
        foreach ($Key in $Boxes.Keys) { $Request[$Key] = $Boxes[$Key].Text }
        $Result = (($Request | ConvertTo-Json -Depth 6 -Compress) | & $ProjectPython $Helper save | Out-String) | ConvertFrom-Json
        if (-not $Result.ok) {
            [Windows.Forms.MessageBox]::Show($Result.detail, '请检查这一项') | Out-Null
            return
        }
        $script:WizardInitial = (& $ProjectPython $Helper read | Out-String) | ConvertFrom-Json
        $Status.Text = '已保存，正在检查本地配置并导入默认人设，请稍候……'
        [Windows.Forms.Application]::DoEvents()
        # Capture subprocess output locally; never display or export credentials.
        $ApplyScript = Join-Path $PSScriptRoot 'account_setup.ps1'
        $PreviousErrorAction = $ErrorActionPreference
        try {
            # Native libraries can write progress to stderr even when successful.
            $ErrorActionPreference = 'Continue'
            $null = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ApplyScript -Action Apply 2>&1
            $ApplyExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorAction
        }
        if ($ApplyExitCode -ne 0) {
            $Status.Text = '填写内容已保存，配置检查未完成。请告知“配置检查失败”。'
            [Windows.Forms.MessageBox]::Show('内容已保存在本机。配置检查失败，请保留本窗口并告知此提示；无需重新填写密钥。', '配置检查失败') | Out-Null
            return
        }
        $Status.Text = '配置完成。下一步：QQ 登录、音频设备和实际通话测试。'
        $Save.Text = '已完成'
        [Windows.Forms.MessageBox]::Show('配置完成！请回复“配置完成”。接下来进行 QQ 登录和通话测试。', '配置完成') | Out-Null
        $Form.Close()
    } catch {
        [Windows.Forms.MessageBox]::Show('未能完成配置，请保留窗口并告知“配置窗口报错”。不要发送密钥。', '配置未完成') | Out-Null
    } finally {
        $Form.UseWaitCursor = $false
        $Save.Enabled = $true
    }
})
if ($PreviewPath) {
    # Developer preview: render an empty form, never export local credentials.
    foreach ($Box in $Boxes.Values) { $Box.Text = '' }
    $Form.ShowInTaskbar = $false
    $Form.Opacity = 0
    $Form.Show()
    [Windows.Forms.Application]::DoEvents()
    $Form.CreateControl()
    $Form.PerformLayout()
    $Bitmap = New-Object Drawing.Bitmap($Form.Width, $Form.Height)
    $Form.DrawToBitmap($Bitmap, (New-Object Drawing.Rectangle(0, 0, $Form.Width, $Form.Height)))
    $Bitmap.Save($PreviewPath, [Drawing.Imaging.ImageFormat]::Png)
    $Bitmap.Dispose()
} else {
    [void]$Form.ShowDialog()
}
$Form.Dispose()
