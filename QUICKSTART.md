# 人工部署

本版是预览候选，以下流程尚未在全新机器完成。需要登录、驱动安装和真实电话的步骤必须由使用者参与。
全部命令在仓库根目录的 PowerShell 执行。不要对一个正在运行的旧安装覆盖文件。

## 1. 准备环境

安装 Python 3.12+、Git 和 [VB-CABLE](https://vb-audio.com/Cable/)。按官方安装步骤安装驱动，重启后应出现对应播放和录音端点。

```powershell
powershell -NoProfile -File install/00_check.ps1 -Json
powershell -NoProfile -File install/10_deps.ps1 -Video
```

`-Video` 创建独立视频环境。检查失败就读 JSON 中的失败项，先修复再继续。
该依赖脚本只安装 AstrBot 和本项目脚本依赖；GPT-SoVITS 的 PyTorch/CUDA 环境要按下一步单独安装。

## 2. 安装第三方运行环境与模型

- 从 [GPT-SoVITS 官方项目](https://github.com/RVC-Boss/GPT-SoVITS) 获取支持 v2Pro 的推理环境，源码放 `runtime/GPT-SoVITS`。
- 将对应 Python 环境位置填入 `paths.tts_python`。默认模板的 `.venv-tts` 不会自动生成；必须先按上游说明准备。
- 从 [LuckyLillia 官方站](https://luckylillia.com/) 获取带 QQ 图形界面的客户端，放入 `runtime/LL`。源环境使用 LLBot 8.3.2 / QQ 9.9.35.52892，其他组合未验证。不要复制别人登录过的客户端目录。
- 视频抽帧需要 FFmpeg 与 FFprobe，位置由 `paths.ffmpeg`、`paths.ffprobe` 指定。
- 动态形象还需要 CameraEmulator。当前自动安装尚未实现，见发布状态文档。

模型包中的 `models/sovits/dafeiyu_e14_s280.pth` 放入仓库同名目录。
其 SHA256 见 `config/downloads.json`。当前候选尚未发布模型 Release URL，默认权重需要先从随附模型包解压。

```powershell
powershell -NoProfile -File install/20_download.ps1
powershell -NoProfile -File install/verify.ps1 -Stage 2 -Json
```

下载器只安装清单中的模型，不会自动安装 QQ、VB-CABLE、CameraEmulator 或 GPT-SoVITS 的 Python 运行环境。
已存在且哈希正确的文件直接跳过；失败文件只保留 `.part`，不会冒充完成。
清单使用当前生效组合：上游 `s1v3.ckpt` + 微调 SoVITS，避免误用旧 GPT 训练快照改变音色。

## 3. 填账号、密钥和设备

```powershell
powershell -NoProfile -File install/30_config.ps1
```

该命令只创建缺少的本地配置、TTS 配置，并复制源码插件到新的 AstrBot 工作目录；不会覆盖已有不同的插件文件。

编辑 `.env`：填写 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`ASTRBOT_DASHBOARD_PASSWORD`。
编辑 `config.json`：填写 `master.qq`，核实 `paths.*`、模型名称和音频设备名称。
不要把密钥粘到公开 Issue 或截图里。脚本不会读取其他安装的凭据。

`audio.playback_hint` 选择通向虚拟线的播放端点，`audio.capture_hint` 选择 QQ 应使用的虚拟麦克风。
不要让电话录音端点跟着自己的 TTS 回声走。设备列表可用 `scripts/audio_probe.py` 等诊断脚本查看。

```powershell
powershell -NoProfile -File install/verify.ps1 -Stage 3 -Json
```

阶段 3 目前验证配置存在性，不验证余额或 API 在线可用性。需在服务商控制台和实际请求中检查。

## 4. 配置 AstrBot、QQ 和人设

首次运行 AstrBot 后，在其本地面板完成以下设置：

1. 建立 DeepSeek 模型提供商，模型为 `deepseek-flash`，使用自己的密钥。
2. 建立 OneBot v11 反向 WebSocket 平台；在 LLBot 指向 `ws://127.0.0.1:6199/ws`，两端认证参数保持一致。
3. 自己扫码登录 QQ。群聊权限由实际群管理员授予，不能仅靠人设声明。
4. 将 `persona/default.md` 导入默认人设，绑定到使用的平台会话。确认 5 个插件正确加载。
5. 建立本地 GPT-SoVITS TTS 提供商，ID 与 `voice.local_provider_id` 一致；地址、参考音频及参考文字与配置一致。
6. 关闭 AstrBot 内置随机 TTS（`provider_tts_settings.enable=false`），由 voice_picker 控制语音，避免重复回答。

如果要保留源环境的云端克隆音色兜底，必须在自己的 DashScope 账号配置该音色，并把对应 AstrBot 提供商 ID 写入 `voice.cloud_provider_id`。
原作者账号的云端音色 ID 不会随包迁移。只配本地 TTS 时，失败的语音可能退回文字。

B 站可选：通过插件扫码登录自己的账号，填写 `master.session`、`master.bili_uid`、`master.bili_name`，再打开 `features.bilibili`。
分享、评论和私信是实际对外发送操作，检查测试对象后再使用；离线验收不执行这些操作。

## 5. 启动与电话验收

确认上述服务未在别处运行，端口空闲，然后：

```powershell
install\40_start_all.bat
```

启动器返回 `processes-launched-not-verified` 表示已请求启动进程，不能解读为所有功能通过。
端口被占会报错并停止，不会结束旧服务。若先单独运行了 AstrBot 配置面板，应先关闭该实例再启动全套。

QQ 麦克风选择配置中的虚拟录音端点。通话过程中运行：

```powershell
.venv\Scripts\python.exe scripts/audio_doctor.py --json
```

完整体检会合成并播放测试语音。`--quick` 跳过试音，不能据此声称回程通过。
开启形象后选择 CameraEmulator 摄像头，确认画面和口型；未安装摄像头驱动不能仅凭渲染进程存活判成功。

## 6. 记录验收、停用

复制 `config/acceptance.example.json` 到 `data/acceptance.json`，只填写在当前安装实际观察到的结果和测得数值。
覆盖 25 项功能、6 项性能指标、音频体检及 3 段人工体验。跨天记忆需要实际跨天验证。

```powershell
powershell -NoProfile -File install/verify.ps1 -Stage 4 -Json
powershell -NoProfile -File install/verify.ps1 -Stage 6 -Json
install\50_stop_all.bat
```

最终检查目前是对验收记录的完整性和阈值校验，**不是自动完成 25 项真实交互**。
任何缺失项保持失败；不要为了通过而填写未观测数据。
