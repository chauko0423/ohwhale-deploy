# 摄像头候选包的安装

这些脚本随独立摄像头 ZIP 分发，需要与 `BUILD_INFO.json`、DLL 和 EXE 放在同一目录；不能单独在源码目录运行。

1. 将 `ohwhale-camera-v0.1.0a1-unsigned.zip` 完整解压到一个目录。
2. 双击 `Install-camera.cmd`，由使用者处理 Windows 管理员提示。媒体源会复制到 `%ProgramFiles%\OhWhale\CameraEmulator-0.1.0a1`，再注册机器范围的 COM 类。
3. 脚本随后以原使用者身份创建摄像头。选择 QQ 中的 `CameraEmulator`，检查画面；该步骤成功仍不代表通话、口型和音频回程已验收。

该构建未签名。脚本会校验随包二进制哈希；如果发现另一处已注册的 CameraEmulator，或目标目录存在不同内容，就停止，不覆盖它，不停止 Frame Server。摄像头隐私权限由使用者处理；脚本不修改这些设置、不添加开机启动、不采集屏幕。

检查包与已有注册状态而不安装：`powershell -NoProfile -File Install-camera.ps1 -CheckOnly`。构建主机已检查过二进制的直接帧输出；第二台设备已完成安装和角色取帧，但 QQ 仍显示实体镜头；本版将其列为未解决的兼容性问题。

移除本用户的摄像头可运行已安装目录中的 `CameraEmulator.exe --uninstall`。机器范围的 COM 类需要管理员注销同目录 DLL；本候选包不自动删除 ProgramData 下可能已有的图像和设置。
