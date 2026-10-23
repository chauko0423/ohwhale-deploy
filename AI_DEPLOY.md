# AI 部署入口

状态：预览候选。全新机器部署未完成实测；不能声称零干预或效果完全一致。
先读 README、QUICKSTART、manifest 和发布状态。凭据、扫码、驱动许可和人工体验必须由使用者完成。

只运行本仓库提供的安装和诊断入口。每步检查 `exit code == 0` 且 JSON 的 `ok == true`。
失败时输出 `BLOCKED: <阶段> <失败项> <本地日志位置>`，说明下一步所需操作，不继续依赖该阶段的步骤。
不要杀死已有服务来腾出端口，不从其他安装复制 Cookie、密钥、聊天数据库或云音色 ID。

| 阶段 | precheck | action | verify | rollback |
|---|---|---|---|---|
| 0 环境 | 当前目录是新安装目录 | `install/00_check.ps1 -Json` | 系统、Python、磁盘检查 | 无修改 |
| 1 依赖 | 阶段 0 通过 | `install/10_deps.ps1 -Video` | `install/verify.ps1 -Stage 1 -Json` | 保留日志；按安装目录范围移除新建 venv 后重试 |
| 2 运行环境与模型 | 上游 TTS 环境、客户端按官方步骤安装 | `install/20_download.ps1` | `install/verify.ps1 -Stage 2 -Json` | `.part` 可继续下载；只处理本次下载文件 |
| 3 配置 | 使用者提供密钥、账号和设备选择 | `install/30_config.ps1`，填本地配置 | `install/verify.ps1 -Stage 3 -Json`；此处仅检查存在性 | 使用本地备份恢复，不能上传凭据 |
| 4 登录与人设 | 按 QUICKSTART 配置 AstrBot 平台和提供商 | 使用者扫码，导入人设，`install/40_start_all.bat` | `install/verify.ps1 -Stage 4 -Json`；需当前安装的人工确认记录 | `install/50_stop_all.bat` 停止本包记录的进程 |
| 5 音频 | 通话已接通，参与者知道要播放测试语音 | 完整 audio_doctor | `install/verify.ps1 -Stage 5 -Json` | 停止本包并恢复使用者原来的默认设备 |
| 6 复现 | 新机器上逐项实测并记录 | 填 `data/acceptance.json` | `install/verify.ps1 -Stage 6 -Json` | 保留失败证据、回到对应阶段修复 |

阶段 2 尚有人工环节：GPT-SoVITS 环境、QQ 客户端、VB-CABLE、CameraEmulator；当前候选模型 Release URL 尚未发布。
阶段 4 的面板配置、人设导入和 cron 创建也尚未自动化。不得伪造这些步骤已完成。

性能阈值来自项目目标：首音 ≤2.5s、ASR ≤0.3s、TTS 首块 ≤0.5s、误打断率 ≤10%、形象 ≥15fps、平均电话回复 ≤35 字。
`verify -Stage 6` 只校验输入记录；输入记录应附测量方法、机器配置、时间和日志，不能拿原环境旧结果冒充新安装结果。
