# 变更记录

## 未发布 · 通话链路稳定性

- 新增 `docs/08_通话链路排障.md`：裸 PCM 采样率与音色变调、流式 ASR 卡 40s、
  静音导致「她聋了」、视频接听不开摄像头、保活/自启的排查与修法。
- 新增可选脚本 `scripts/asr_local_server.py`：本地 faster-whisper 常驻 HTTP ASR
  （短句约 0.6s，不依赖网络），作为云端识别卡顿时的兜底。
- 新增可选脚本 `scripts/tts_cosyvoice_shim.py`：GPT-SoVITS 兼容的 `/tts` 接口，
  后端换成百炼 CosyVoice；严格区分 `media_type=wav` 与 `media_type=raw`
  （raw 输出必须按 `audio.tts_raw_rate`，否则会被播成低沉的慢速人声）。
- `docs/05_故障排查.md` 增加指向新文档的入口。


## 未发布 · 形象链路修复

- 新增 `scripts/avatar_to_camera.py`：把 `avatar_live.py` 的共享内存帧按 10 fps 写成
  `%ProgramData%\CameraEmulator\image.png`。摄像头源每帧重读该文件，因此
  **普通权限运行 avatar_live（Local 段）也能显示动态形象**，不要求管理员。
- `ohwhale/deploy.py`：`features.avatar` 开启时同时启动上述桥接（`avatar_bridge`）。
- `scripts/avatar_live.py`：启动时打印实际使用的共享内存名字；使用 Local 段时明确提示
  “跨会话摄像头服务读不到”，并指向桥接方案。
- `install/camera/Install-camera.ps1`：安装时放开 `%ProgramData%\CameraEmulator` 的
  Users 修改权限。此前该目录默认只读，`CameraEmulator.exe` 会报
  `Saving the layout failed: 0x80070005` 并静默退出，表现为摄像头一直显示占位图。
- `docs/05_故障排查.md`：补充“占位图 / Global 与 Local / QQ 仍用实体镜头”的排查与回退步骤。

> 仍未解决：部分设备 QQ 选择 CameraEmulator 后依旧使用实体摄像头。
> 文档给出的是人为禁用实体摄像头的绕过方式，不算 QQ 视频链路验收通过。

## 0.1.0a1 · Alpha 预览版（2026-10-24）

- 抽离 23 个原有脚本、5 个插件、当前默认人设、362 张表情包及参考音频和形象素材。
- 配置外置，移除本机密码、账号和运行路径；表情索引改为相对路径。
- 修复看门狗未定义客户端变量，移除按进程名全局结束 QQ 的逻辑。
- B 站回归测试使用临时假 Cookie，不再依赖真实登录态。
- 下载器加入续传、来源回退、大小/SHA256 校验；上游模型锁定版本。
- 明确当前 TTS 默认组合为上游 s1v3 + 微调 SoVITS。
- 增加安装入口、分阶段 JSON 检查和部署文档。第二台设备已确认文字、语音消息、两轮通话和挂断重拨；从零部署复验、重启恢复、长期运行及 QQ 视频仍未完成。

- 发布独立默认 SoVITS 模型及 SHA256 校验。
- 已知问题：部分设备 QQ 选择 CameraEmulator 后仍显示实体镜头；保留为未解决问题，未确认与实体摄像头共存的因果关系。
