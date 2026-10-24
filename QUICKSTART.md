# 人工部署

本版是预览候选，以下流程尚未在全新机器完成。需要登录、驱动安装和真实电话的步骤必须由使用者参与。
全部命令在仓库根目录的 PowerShell 执行。不要对一个正在运行的旧安装覆盖文件。

## 独立设备安装检查

候选测试包放到一个新目录后，双击根目录的 `Run-device-test.cmd`。它使用锁定并校验 SHA256 的 Python 3.12.15 独立发行包，依次安装主环境、TTS 环境、QQ 客户端文件和模型，再生成一段完整语音及三段流式语音。Python 放在本项目 `runtime/Python312` 下，不设置系统 PATH。首次下载有数 GB，需保持联网。

测试包须包含 `models/sovits/dafeiyu_e14_s280.pth`；源码包用户先解压随附模型包。日志在 `data/device-setup.log`，进度和结果在 `data/device-setup.json`，语音测量在 `data/tts-smoke.json`。同目录的 WAV 文件供人工试听。失败后重新运行会复用已完成的下载和依赖，不需要删除安装目录。

如果 PyPI 下载很慢，可停止当前安装后运行 `Run-device-test-mirror.cmd`。它仅为本次进程使用[清华 PyPI 镜像](https://mirror.tuna.tsinghua.edu.cn/help/pypi/)，不修改系统 pip 设置；PyTorch CUDA 仍使用官方专用源，模型仍使用锁定清单中的来源。不要同时运行两个安装器。

这一步不登录 QQ、不配置密钥、不安装音频或摄像头驱动。`ok=true` 只表示依赖和 TTS 检查完成；真实通话、音频回程、形象及完整功能验收仍按下文进行。

## 1. 准备环境

安装 Python 3.12 x64、Git 和 [VB-CABLE](https://vb-audio.com/Cable/)。按官方安装步骤安装驱动，重启后应出现对应播放和录音端点。建议为环境、下载缓存和模型保留至少 25 GiB 空间。

无 Python 时也可先运行 `install/00_machine.ps1` 生成硬件报告。安装脚本跳过 WindowsApps 商店占位程序；找不到 Python 时，可将 `OHWHALE_PYTHON` 设置为自己的 Python 3.12 可执行文件路径。

```powershell
powershell -NoProfile -File install/00_check.ps1 -Json
powershell -NoProfile -File install/10_deps.ps1 -Video
```

`-Video` 创建独立视频环境。检查失败就读 JSON 中的失败项，先修复再继续。
该依赖脚本只安装 AstrBot 和本项目脚本依赖；GPT-SoVITS 使用下一步的独立环境。

## 2. 安装第三方运行环境与模型

- 运行 `powershell -NoProfile -File install/15_tts.ps1`：下载锁定的 GPT-SoVITS 源码，校验并应用六个现用推理修补，建立 `.venv-tts`，安装 PyTorch 2.9.1 CUDA 12.8 及中文推理依赖。需要兼容的 NVIDIA 驱动；脚本最后检查 CUDA 是否可用。已在第二台 RTX 2060 6 GB 设备完成依赖、CUDA 及中文合成检查；完整功能仍待验收。
- 源码和修补清单在 `config/runtimes.json`；修补说明在 `third-party/gpt-sovits-overrides/README.md`。不会覆盖未知的已有源码修改。
- 运行 `install/16_qq_client.ps1`，从上游 v8.3.2 Release 下载、校验并解压 LuckyLillia Desktop 到 `runtime/LL`。还需安装官方 QQ，并在 LuckyLillia 选择有头模式；该模式提供实际电话窗口。源环境使用 QQ 9.9.35.52892，其他组合未验证。不要复制别人登录过的客户端目录。
- 视频抽帧运行 `install/17_video_tools.ps1`，下载并校验 Gyan 的 FFmpeg 9.0.2 构建，完整保留随包许可。默认安装到 `runtime/ffmpeg`，位置由 `paths.ffmpeg`、`paths.ffprobe` 指定。
- 动态形象还需要 CameraEmulator。独立摄像头候选 ZIP 提供 `Install-camera.cmd`，校验后注册媒体源并创建当前用户摄像头；完整安装仍待第二台设备实测，见 `install/camera/README.md`。

模型包中的 `models/sovits/dafeiyu_e14_s280.pth` 放入仓库同名目录。
其 SHA256 见 `config/downloads.json`。当前候选尚未发布模型 Release URL，默认权重需要先从随附模型包解压。

```powershell
powershell -NoProfile -File install/20_download.ps1
powershell -NoProfile -File install/verify.ps1 -Stage 2 -Json
```

`20_download.ps1` 只安装清单中的模型。QQ 客户端和 FFmpeg 使用各自安装脚本；VB-CABLE、CameraEmulator 尚需另外安装。
已存在且哈希正确的文件直接跳过；失败文件只保留 `.part`，不会冒充完成。
清单使用当前生效组合：上游 `s1v3.ckpt` + 微调 SoVITS，避免误用旧 GPT 训练快照改变音色。

## 3. 填账号、密钥和设备

推荐打开 `account-setup` 文件夹，双击 `1-Edit-local-config.cmd`。中文窗口集中填写主人 QQ、DeepSeek 密钥、百炼密钥和面板密码，每项都有用途说明。点击“保存并完成配置”即可检查必填项并导入 AstrBot 配置与人设，不必再运行第二个脚本。此前填写过的内容会自动带入，密钥默认隐藏；窗口打开后若配置文件被其他程序修改，将停止保存并提示重开。已经建立的 AstrBot 账号配置需在面板调整，窗口不会静默覆盖。此步骤不调用付费 API、不发送 QQ 消息，实际账号在线验证仍需后续完成。

以下为手动配置方式；`.env` 模板有逐项中文注释，`config.json` 顶部的 `master.qq` 是主人 QQ。JSON 不支持注释，字段说明见 `account-setup/填写说明.txt`。

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

先填写 `.env` 和 `config.json`，再对新的 AstrBot 工作目录执行：

```powershell
powershell -NoProfile -File install/31_astrbot.ps1
```

脚本建立 DeepSeek、本地 GPT-SoVITS、OneBot 平台和主人账号配置，并导入默认人设。面板与 OneBot 监听本机回环地址，默认仅放行主人的私聊；群聊放行需在面板明确配置。密码以 AstrBot 支持的哈希格式保存。重复运行会核验已有结果；检测到手工修改时停止，不覆盖配置。该步骤不创建任何定时外发任务。

首次启动后，在本地面板核对：

1. 建立 DeepSeek 模型提供商，模型为 `deepseek-flash`，使用自己的密钥。
2. 建立 OneBot v11 反向 WebSocket 平台；在 LLBot 指向 `ws://127.0.0.1:6199/ws`，两端认证参数保持一致。
3. 自己扫码登录 QQ。群聊权限由实际群管理员授予，不能仅靠人设声明。
4. 将 `persona/default.md` 导入默认人设，绑定到使用的平台会话。确认 5 个插件正确加载。
5. 建立本地 GPT-SoVITS TTS 提供商，ID 与 `voice.local_provider_id` 一致；地址、参考音频及参考文字与配置一致。
6. 关闭 AstrBot 内置随机 TTS（`provider_tts_settings.enable=false`），由 voice_picker 控制语音，避免重复回答。

LuckyLillia/PMHQ 首次启动还可能要求 **Auth Token**（上游授权），它不是 `.env` 中的 `ONEBOT_ACCESS_TOKEN`，也不是 DeepSeek/百炼密钥。按客户端提示由使用者在上游完成授权；项目不领取、重置或迁移该凭据。若重新领取可能影响现用设备，停在此步，保留现有授权，不把 QQ 登录或通话标为通过。上游相关说明见 [启动授权讨论](https://github.com/LLOneBot/LuckyLilliaBot/issues/793)。

LuckyLillia 打开窗口不代表 Bot 已启动。需要在控制面板点击“启动”，或由使用者确认后启用“启动软件后自动启动 Bot”。OneBot 反向连接也需单独启用；配置地址只是填写目标，不代表已经连接成功。

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

分步验收时，如果 QQ 已单独登录，可运行 `.venv\Scripts\python.exe -m ohwhale.deploy start --core-only`，仅启动 AstrBot 与 TTS，保留现有 QQ，不启动电话、形象或麦克风看守。完成文字与语音消息检查后，用 `install\50_stop_all.bat` 停止这两个由启动器记录的服务，再准备音频设备并进行全套启动。不要在它们仍运行时再次执行全套启动。

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
