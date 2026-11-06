# mpv-audio

独立的跨平台音频版 **libmpv** 构建与发布项目。运行时只需要库文件，不启动 mpv / FFmpeg 可执行程序。

## 发布与自动更新

- 每天 UTC 03:23 检查 [mpv 上游最新稳定版](https://github.com/mpv-player/mpv/releases)，也支持在 Actions 中手动运行 **Native audio release**。
- 将上游稳定版 tag 解析为不可变的源码 commit，生成本次构建的 `native-lock.json`。
- 对 Linux、macOS、Windows 的 amd64 / arm64 共六个平台分别原生构建。
- 检查 FFmpeg 没有启用视频解码器；运行动态库版本、WAV 解码、频谱滤镜和 PCM 采样冒烟测试。
- 在本仓库使用合成音频独立验证 MP3 / AAC / FLAC / Vorbis / Opus、通过本地 HTTP 提供的 HLS、24 段真实频谱元数据、暂停 / 音量及 PCM 采样。所有回归测试均在本仓库内完成。
- **六个平台全部通过后才发布** `libmpv-audio-<mpv版本>-r<修订号>`。失败保留 Actions 日志，不发布也不覆盖旧版本。
- 相同版本的构建配置调整使用新的修订号，例如 `r2`。流水线禁止覆盖已有 tag / Release，不使用 `--clobber`。

源码更新不需要维护 mpv 分叉；音频 profile 位于 `scripts/build_native.py`。检查结果是每次 Release 随附的 lock，不会为了更新版本自动修改主分支代码。

GitHub 定时任务只在默认分支运行，可能延迟；长期不活跃的公开仓库可能被 GitHub 暂停定时任务，届时需重新启用。

## 构建内容

当前基线：mpv **0.42.0**、FFmpeg **8.0.1**、libplacebo **7.387.0**、libass **0.17.4**，源码 commit 均固定在 `native-lock.json`。未来只自动跟进 mpv；如果新 mpv 需要升级依赖，构建明确失败，维护者审核更新依赖锁后再重试。

保留：
- MP3 / MP2、AAC / HE-AAC、FLAC、Vorbis、Opus、ALAC、PCM 等音频解码；
- HTTP / HTTPS、HLS、MPEG-TS / MP4 等音频封装；
- 24 段频谱需要的 `bandpass / join / astats / pan` 等滤镜；
- WASAPI（Windows）、CoreAudio（macOS）、ALSA / PulseAudio（Linux）；
- null 输出和 PCM 输出，用于无声卡测试及音频采样。

关闭 FFmpeg 视频解码器、编码器、程序，以及 mpv 的 CLI、视频输出、硬件视频解码、脚本等可选功能。mpv 仍有强制的视频基础代码及 libass / libplacebo / swscale 依赖；本项目是**音频功能裁剪构建**，不是保证完全不含视频相关代码的 mpv 分叉。

TLS 使用 Windows Schannel、macOS Secure Transport 或 Linux GnuTLS。

### 平台范围与复现边界

| 平台 | 构建环境 / 最低范围 |
| --- | --- |
| Windows amd64 | Windows 2025 runner，MSYS2 UCRT64 |
| Windows arm64 | Windows 11 ARM runner，MSYS2 CLANGARM64 |
| macOS amd64 / arm64 | macOS 15；所有打包依赖必须不高于 macOS 15 |
| Linux amd64 / arm64 | Ubuntu 24.04；更旧 glibc 及其他发行版需另行验证 |

四个主要组件源码固定；字体、TLS、系统音频等辅助库来自构建环境。其实际版本、来源信息和许可证记录在 bundle 的 `manifest.json` / `licenses/packages/`。**目前不承诺所有辅助依赖或工具链的字节级可复现性**；已发布包及校验值固定，重建必须使用新修订号。CI 无声卡测试不等于真实声卡播放或最低系统版本实机验证。

## Release 产物

- `mpv-audio-<os>-<arch>.zip`：包含 `mpv/` 下主库、全部非系统动态依赖、许可证、构建配置和冒烟报告。
- `sources-<os>-<arch>.tar.gz`：本次四个主要组件的完整源码快照，包含固定的 Git 子模块；不包含辅助发行版库的源码。
- `native-lock.json`：本次四个主要组件的源码 commit。
- `release.json`：六个平台运行包的 SHA-256。
- `SHA256SUMS`：所有发布资产的 SHA-256。

主库名称分别为 `mpv-2.dll`、`libmpv.dylib`、`libmpv.so.2`。**不要只复制主库**；依赖必须保留在同一目录。Linux 写入 `$ORIGIN` RPATH，macOS 改写 `@loader_path` 并执行 ad-hoc 签名。

### 集成使用

选择目标系统和架构的运行包，固定 **Release tag + 运行包 SHA-256**，下载并校验后解压。通过 libmpv C API 动态加载主库，并保留同目录下的全部动态依赖。版本升级时显式更新 tag 和校验值，不引用 `latest`，也不混用本机安装的其他版本。

SHA-256 防止内容意外变化，不等同于独立签名；应信任本仓库及发布工作流。仓库所有者仍可手动删除或修改 GitHub Release，集成方的固定校验值会拒绝改变后的包。

## 本地开发

Python 3.11+：

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

原生构建还需要 C/C++ 编译器、Git、make、pkg-config、Ninja 和各平台字体 / 音频 / TLS 开发库；具体安装列表见 `.github/workflows/native-release.yml`。发行版 FFmpeg 可执行程序仅用于生成合成测试样本，不进入发布包，也不替代从锁定源码构建的音频版 FFmpeg 库。

```bash
python -m pip install -r requirements.txt
python scripts/build_native.py --platform linux/amd64 --lock native-lock.json
python scripts/fixtures.py --output build/fixtures
python scripts/smoke.py --bundle build/linux-amd64/bundle/mpv --fixtures build/fixtures --lock native-lock.json
# 独立原生回归全部通过后再打包：
python scripts/package.py --platform linux/amd64
```

构建拒绝跨系统 / 架构运行，也拒绝复用已有构建目录，避免混入旧组件。手动检查后可删除 `build/<os>-<arch>` 再重建。

## 许可与来源

本项目构建脚本和测试以 [AGPL-3.0](LICENSE) 发布。mpv、FFmpeg、libass、libplacebo 和辅助依赖分别遵循各自许可证，并非全部改用 AGPL。

运行包收集许可证与来源信息；源码包提供四个固定组件的实际源码。**这不替代分发者的合规审核**：还需要为打包的辅助库提供其许可证要求的对应源码、构建材料或合规来源方式，检查最终许可证兼容性。辅助库的包管理器元数据不自动等于完成了源码分发义务。
