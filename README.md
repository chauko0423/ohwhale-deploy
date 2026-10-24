# 哦鲸鲸部署包 · OhWhale

把 QQ 聊天、语音通话、默认音色、虚拟形象和 B 站工具整理成可以独立配置的 Windows 项目。

**当前状态：0.1.0a1 本地预览候选。源码与离线测试已整理；全新机器部署、真实通话和体验对等尚未验收。**
原运行环境的成功记录不能替代本包的换机验证。本版不承诺 40 分钟装完或固定响应延迟。

| 入口 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | 人工配置与运行步骤 |
| [AI_DEPLOY.md](AI_DEPLOY.md) | AI 分阶段部署、检查、停止条件 |
| [manifest.yaml](manifest.yaml) | 服务、依赖、必需配置和验收入口 |
| [docs/05_故障排查.md](docs/05_故障排查.md) | 按症状定位问题 |
| [docs/07_发布状态.md](docs/07_发布状态.md) | 已完成、未验证及发布前缺口 |

## 需要准备

| 项目 | 用途 |
|---|---|
| Windows x64、Python 3.12 x64 | 脚本环境；当前源环境为 Windows 11 |
| DeepSeek API Key | 聊天及视频图像理解；账号按服务商规则计费 |
| DashScope API Key | 流式语音识别、电话截图理解；云端克隆音色需要单独配置 |
| QQ 账号 | 使用者自己扫码；建议与日常账号分开 |
| LuckyLillia/PMHQ 启动授权 | 客户端可能要求 Auth Token；与 OneBot 连接 token 不同，不能由项目代发 |
| VB-CABLE | 本地 TTS 到 QQ 麦克风的音频回程 |
| GPT-SoVITS 推理环境 | 默认音色的本地合成；显卡、驱动、PyTorch 组合需实测 |
| B 站账号（可选） | 登录后的私信、点赞、评论等工具 |
| CameraEmulator（形象可选） | 当前动态形象使用的共享内存虚拟摄像头；不是仅装 OBS 即可 |

第三方 QQ 协议适配器可能遇到账号限制、接口变动或平台风控。不要用本项目规避平台限制。

## 包含什么

- 23 个抽离的原有脚本、5 个 AstrBot 插件，以及统一配置、安装、校验和离线测试工具。
- 当前默认人设文本；362 张表情包、12 个情绪类别和可迁移索引；默认参考音频与形象素材。
- 本地权重以独立模型包交付，不放入 Git；上游模型从固定版本下载并核对 SHA256。
- 密钥填在本地 `.env`；账号、设备和路径填在本地 `config.json`。

电话默认直接调用模型以保留源环境的低延迟路径。它读取人设文本，**不自动等于 AstrBot 的跨天记忆**。
B 站自动巡检、形象和默认麦克风看守默认关闭；完成对应配置后自行启用。

## 开发验证

在仓库根目录安装可编辑包后运行：

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/test_bili_patch.py
python plugins/astrbot_plugin_qq_power/test_qq_power.py
python scripts/release_check.py
```

上述检查不发送 QQ/B 站消息，不代表电话、账号登录或新机器部署已经通过。

## 许可证与素材

项目原创代码按 [MIT](LICENSE) 发布。依赖保持各自许可证，见 [NOTICE](NOTICE)。
素材的制作方说明、来源核对状态与逐文件哈希见 [assets/ASSETS.md](assets/ASSETS.md)。
不会用项目的 MIT 声明替第三方改写授权范围。

这是软件驱动的虚拟角色。不要冒充现实中的个人，不要把它当成专业医疗、法律或紧急服务。
停用方式和本地记忆位置见 [docs/04_人设与记忆.md](docs/04_人设与记忆.md)。
