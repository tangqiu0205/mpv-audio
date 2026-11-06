# mpv-audio

独立的跨平台音频版 **libmpv C SDK** 构建与发布项目。不绑定编程语言、界面框架或应用；运行时只加载动态库，不启动 mpv / FFmpeg 可执行程序。

## 平台与产物

| 目标 | 产物 | 构建与兼容性范围 |
| --- | --- | --- |
| Windows amd64 / arm64 | 各一个 `mpv-audio-windows-<arch>.zip` | Windows 2025 / Windows 11 ARM runner，MSYS2 UCRT64 / CLANGARM64 |
| Linux amd64 / arm64 | 各一个 `mpv-audio-linux-<arch>.zip` | Ubuntu 24.04；更旧 glibc 和其他发行版需另行验证 |
| macOS amd64 / arm64 | 各一个 `mpv-audio-darwin-<arch>.zip` | macOS 15；包含的依赖也必须满足此最低版本 |
| macOS universal | `libmpv_macos.xcframework.zip` | 同时包含 arm64 / x86_64，供 Xcode 集成 |

这些是流水线的目标产物；**只有对应 Actions 全部成功后的 Release 才是可下载的已验证构建**。目前不构建移动端或 Web，不因构建机依赖升级而自动提高最低系统要求。

分架构 SDK 包含：
- `mpv-2.dll`、`libmpv.so.2` 或 `libmpv.dylib`，以及全部非系统动态依赖；
- `include/mpv/` 下的上游 C 头文件；
- 可重定位的 `lib/pkgconfig/mpv.pc`，版本号使用 libmpv 的 **C API 版本**；
- Windows 的 `libmpv.dll.a` 和 `mpv.def`；导入库引用实际分发的 `mpv-2.dll`；
- `examples/version.c`、原生回归报告、C SDK 编译 / 链接 / 运行报告；
- 许可证、来源和构建配置。

主库和依赖必须保留在同一目录。Linux 使用 `$ORIGIN` RPATH；macOS 主库使用 `@rpath` install ID，同目录依赖使用 `@loader_path`。

XCFramework 内的 `mpv.framework` 包含 universal 主库、头文件，以及嵌套在 `Frameworks/` 中的 universal 动态依赖。两个架构的源码锁、头文件和依赖集合不一致时拒绝合并。构建会验证合并后的架构、音频行为，以及真实 C 程序对 XCFramework 的编译 / 链接 / 运行。签名是 ad-hoc，不是 Developer ID 签名，也未经公证。

## 自动更新与发布门槛

每天 UTC 03:23 检查 [mpv 上游最新稳定版](https://github.com/mpv-player/mpv/releases)，也支持手动运行 **Native audio release**：

1. 将上游稳定版 tag 解析为源码 commit，生成本次 `native-lock.json`。
2. 在独立任务中生成一组合成音频，让所有平台复用；不依赖各平台测试编码器是否齐全。
3. 六个平台原生构建，并检查没有意外启用视频解码器、GPL / nonfree 配置。
4. 使用无声卡输出验证 WAV、MP3、AAC、FLAC、Vorbis、Opus、本地 HTTP HLS、HTTPS 音频传输、24 段真实频谱、暂停 / 音量和 PCM 采样。
5. 从运行包的头文件和库编译、链接并执行真实 C 示例，验证 SDK 可用性。
6. 合并并验证 macOS universal XCFramework。
7. 全部通过后自动发布 **prerelease 候选版** `libmpv-audio-<版本>-r<修订号>`；失败只保留日志和诊断产物，不发布。

候选版完成最低系统版本、真实音频设备、集成兼容性及全部依赖许可审核后，由维护者在 GitHub Release 页面手动取消 prerelease 标记，转为稳定版。自动任务不会擅自将候选版提升为稳定版。

相同版本的配置调整使用新的修订号，例如 `r2`。流水线不覆盖已有 tag / Release，不使用 `--clobber`。已存在的候选版也不会自动重建。

GitHub 定时任务仅在默认分支运行，可能延迟；长期不活跃的公开仓库可能被暂停定时任务，需要重新启用。

## 音频构建配置

基线为 mpv **0.42.0**、FFmpeg **8.0.1**、libplacebo **7.387.0**、libass **0.17.4**，源码 commit 固定在 `native-lock.json`。未来只自动跟进 mpv；如果新版本要求升级依赖，构建明确失败，由维护者审核依赖锁后重试。

源码补丁记录在 `patches/` 和源码锁中，应用前执行严格检查，并将补丁及 SHA-256 一并收入源码包和许可证目录。当前补丁修正关闭 Cocoa 时 CoreAudio 遗漏 `osdep/utils-mac.c` 的上游构建依赖，不引入 GUI。libmpv 使用严格的未定义符号链接检查；补丁与未来上游不兼容时会明确停止，而不是静默忽略。

保留 MP3 / MP2、AAC / HE-AAC、FLAC、Vorbis、Opus、ALAC、PCM 等音频解码，HTTP / HTTPS、HLS、MPEG-TS / MP4 等封装，以及频谱滤镜、null / PCM 输出。系统音频后端为 WASAPI、CoreAudio、ALSA / PulseAudio；TLS 使用 Schannel、Secure Transport 或 GnuTLS。

关闭 FFmpeg 编码器、视频解码器、程序，以及 mpv CLI、视频输出、硬件视频解码和脚本等可选功能。mpv 仍包含强制的视频基础代码和 libass / libplacebo / swscale 依赖；这是**音频功能裁剪构建**，不是完全删除视频代码的源码分叉。

## LGPL 配置与许可边界

- mpv 使用 `-Dgpl=false`，并检查生成的 `HAVE_GPL=0`。
- FFmpeg 使用 `--disable-gpl --disable-nonfree`，并检查生成的 `CONFIG_GPL=0` / `CONFIG_NONFREE=0`。
- 保留四个主要组件的许可证、完整源码快照和实际配置；每个 bundle 记录辅助依赖的包版本、来源和可收集的许可证。
- manifest 中的 `license_profile: lgpl` 表示已验证的**构建配置**，不是对全部分发材料的法律认证。mpv 上游也明确指出，编译开关本身不构成 LGPL 授权。

四个主要组件源码固定；字体、TLS、系统音频等辅助库来自构建环境。其许可证仍需逐项核对，manifest 明确标注 `supporting_licenses_review_required`。目前不承诺辅助依赖和工具链的字节级可复现性。

运行包与主库允许独立替换。分发者必须履行每个组件适用的许可义务，包括提供许可证要求的对应源码、修改和构建材料。源码资产只包含四个主要组件及其固定子模块，不包含发行版辅助库源码；包管理器元数据不等于完成了源码分发义务。

本项目的构建脚本、测试和示例采用 [AGPL-3.0](LICENSE)。**脚本的许可证不等于生成动态库的许可证**；动态库及其各组件分别遵循相应许可。

## 集成 SDK

固定 **Release tag + 运行包 SHA-256**，校验后解压；不引用 `latest`，也不混入本机其他版本的库。

Linux 的一个示例：

```bash
export PKG_CONFIG_PATH="$PWD/mpv/lib/pkgconfig"
cc mpv/examples/version.c $(pkg-config --cflags --libs mpv) \
  -Wl,-rpath,"$PWD/mpv" -Wl,-rpath-link,"$PWD/mpv" -o version
./version
```

macOS 直接加载 `.dylib` 时配置应用的 `@rpath`；Xcode 工程可添加 XCFramework，并正确嵌入其 framework 和动态依赖。发布应用时仍需按自己的签名、公证和最低系统要求验证。

Windows 可使用 MinGW 导入库链接，或按 `mpv.def` 为目标工具链生成导入库；也可通过标准动态加载接口调用 C API。运行时确保主库及依赖能从同一受控目录加载。

Release 附带 `native-lock.json`、`release.json`、全部资产的 `SHA256SUMS` 和各平台源码包。SHA-256 不等同于独立签名；仓库所有者仍能手动修改 Release，但固定校验值会拒绝变更后的文件。

## 本地开发

需要 Python 3.11+。构建还需要对应系统 / 架构的编译器、Git、make、pkg-config、Ninja 和开发库，安装列表见 `.github/workflows/native-release.yml`。

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
python -m pip install -r requirements.txt
python scripts/build_native.py --platform linux/amd64 --lock native-lock.json
# 生成样本的环境必须有包含相应音频编码器的 FFmpeg；CI 在 Ubuntu 中统一生成。
python scripts/fixtures.py --output build/fixtures
python scripts/smoke.py --bundle build/linux-amd64/bundle/mpv --fixtures build/fixtures --lock native-lock.json
python scripts/sdk.py --check build/linux-amd64/bundle/mpv
python scripts/package.py --platform linux/amd64
```

样本生成使用的 FFmpeg 可执行程序不进入运行包，也不替代锁定源码构建的音频库。构建拒绝跨系统 / 架构运行及复用已有构建目录；手动检查后可删除相应 `build/<os>-<arch>` 再重建。CI 无声卡测试不等于真实声卡或最低系统版本实机验证。HTTPS 回归使用本地自签名服务并明确关闭客户端证书校验，只验证加密传输与解码，不代表公共 CA、主机名或证书失败路径已验证。
