# CameraEmulator 的视频格式修补

上游为 https://github.com/jnhaas/CameraEmulator ，锁定提交与源码包 SHA256 见 `config/camera-source.json`。42 个上游文件与 Git 树核对通过；两个 BAT 文件只存在归档换行差异。MIT 许可证在本目录保留。

本项目只改 `src/source/MediaStream.cpp` 的媒体格式列表，提供 NV12/RGB32 的 1280×720、1920×1080，复现原运行环境的横向 QQ 画面设置。其他构建源码使用锁定上游版本。

## 重建

1. 下载清单中的源码 ZIP，先核对大小和 SHA256，解压成 `runtime/CameraEmulator-src`。
2. 核对原始 `src/source/MediaStream.cpp` 的 SHA256，再用本目录同路径文件替换，并核对修补后 SHA256。
3. 在有 Visual Studio 2022 C++ 工具链和 Windows 11 SDK 的机器运行：

```powershell
cmake -S runtime/CameraEmulator-src -B runtime/CameraEmulator-build -A x64 -DCMAKE_CXX_FLAGS=/utf-8 -DCMAKE_DISABLE_FIND_PACKAGE_Git=TRUE
cmake --build runtime/CameraEmulator-build --config Release
```

禁用 Git 查找是为了不把外层 OhWhale 仓库提交误记为上游提交；二进制显示 `1.0.0.0 (local)`，实际来源由随包 `BUILD_INFO.json` 记录。编译产物尚未签名，不把它冒充为上游官方安装包。

本地主机已用上游 SourceTest 直接加载 DLL，在 720p 和 1080p 下各读取 60 帧，均约 30.8 fps。这不需要注册摄像头，也没有替换已有 DLL。该结果仅覆盖媒体源直接读取；CHUAKO 驱动注册、QQ 摄像头选择、动态口型仍待实测。
