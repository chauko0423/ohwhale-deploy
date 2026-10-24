# 默认音色模型

模型文件不进 Git。当前推理组合为上游 `s1v3.ckpt` 和 `dafeiyu_e14_s280.pth`。
上游文件由 `config/downloads.json` 锁定来源、版本和 SHA256；微调 SoVITS 由随附模型包或 [v0.1.0a1 Release](https://github.com/chauko0423/ohwhale-deploy/releases/tag/v0.1.0a1) 提供。
不要默认替换为微调 GPT epoch 快照：源环境记录显示，这会改变短句稳定性。
