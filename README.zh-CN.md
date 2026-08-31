<p align="center">
  <img src="assets/brand/agentseat-hero.svg" alt="AgentSeat——一个应用，两位操作者" width="100%">
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<h1 align="center">AgentSeat</h1>

<p align="center">
  <strong>在一个本地应用里，为 AI Agent 提供独立操作席位。</strong><br>
  Agent 有自己的指针和键盘，你的仍然属于你。
</p>

<p align="center">
  <a href="https://github.com/vimalinx/AgentSeat/releases/latest"><img alt="GitHub 发行版" src="https://img.shields.io/github/v/release/vimalinx/AgentSeat?style=flat-square&amp;color=57b8ed"></a>
  <a href="LICENSE"><img alt="MIT 许可证" src="https://img.shields.io/badge/license-MIT-080c11?style=flat-square"></a>
  <img alt="Wayland 原生" src="https://img.shields.io/badge/Wayland-native-a9dcf7?style=flat-square&amp;labelColor=080c11">
  <img alt="Hyprland 宿主" src="https://img.shields.io/badge/Hyprland-host-a9dcf7?style=flat-square&amp;labelColor=080c11">
</p>

AgentSeat 让人类和 AI Agent 同时操作同一个普通 GUI 应用。它围绕
Agent 的虚拟输入和观察能力建立了一条狭窄的应用边界，而不是在用户外面
再套一个桌面。

| 人类保留 | Agent 获得 | 边界保证 |
| --- | --- | --- |
| 真实指针和键盘 | 私有虚拟指针和键盘 | 不移动宿主指针 |
| 宿主焦点和工作区 | 应用范围内的焦点 | 不切换工作区 |
| 宿主剪贴板 | 私有的一次性 Unicode 粘贴 | 不替换宿主剪贴板 |

## 生来轻量

运行时刻意保持精简：

- 宿主上只有一个普通 Hyprland 窗口；
- 一个精简的单应用 wlroots 微宿主；
- 一个负责观察和限定范围输入的原生事件循环守护进程；
- 没有嵌套 Hyprland、桌面环境、虚拟机、tmux 或逐应用驱动；
- 无须修改 Hyprland 或其配置。

默认运行原生 Wayland 应用，私有 XWayland 通道用于兼容旧版 X11 软件。
对话框和子窗口会留在同一棵私有应用树中。显式启用后，小型协作脑袋也
渲染在这棵树内部，而不是成为全局桌面浮层。

> AgentSeat 隔离的是输入和焦点，不是文件系统、进程或网络沙箱；被封装
> 的程序仍以你的本地用户身份运行。

## 系统要求

当前版本面向以 Hyprland 为宿主的 Linux，需要：

- C17 编译器、CMake、Meson、Ninja 和 GNU gettext 工具；
- wlroots 0.20、Wayland、Wayland protocols 和 libxkbcommon；
- json-c、GTK 4 和 gtk4-layer-shell；
- 运行时安装 `grim` 和 `wl-clipboard`；
- 控制器需要 Python 3.11 或更高版本；
- 仅在使用 `--x11` 时需要 XWayland。

在 Arch Linux 上，对应的软件包包括 `base-devel`、`cmake`、`meson`、
`ninja`、`wlroots0.20`、`wayland`、`wayland-protocols`、`libxkbcommon`、
`json-c`、`gtk4`、`gtk4-layer-shell`、`gettext`、`grim` 和
`wl-clipboard`。

## 构建与安装

在本目录执行：

```sh
./scripts/build.sh
./bin/agentseat version
```

构建产物保留在 `build/` 下，并在 `bin/` 中生成三个被 Git 忽略的可执行
文件。安装到当前用户：

```sh
./scripts/install.sh
~/.local/bin/agentseat version
```

可以通过 `PREFIX` 安装到其他位置。默认安装目录是
`~/.local/lib/agentseat`，命令符号链接位于 `~/.local/bin`。

## 使用方法

```sh
agentseat run --host-workspace 8 -- /usr/bin/zenity --info --text='你好'
agentseat status
agentseat windows
agentseat move 0.50 0.50
agentseat click
agentseat type '来自 Agent 的问候'
agentseat scroll vertical -120
agentseat observe
agentseat pause
agentseat resume
agentseat stop
```

`run` 直接接收 argv，绝不会交给 shell 求值。默认使用 Wayland；需要通用
私有 XWayland 通道时使用 `run --x11 -- COMMAND ...`。应用必须由 AgentSeat
启动；Wayland 合成器边界不允许把已经运行的宿主窗口直接搬进来。

指针坐标采用从 `0.0` 到 `1.0` 的归一化值。当前 XKB 键盘映射能够表达的
文本通过虚拟键盘事件输入；其他 Unicode 文本通过私有微宿主内的一次性
剪贴板数据提供机制输入，既不读取也不替换宿主剪贴板。

目标工作区默认为 1。目标是非活动工作区时，AgentSeat 会使用一次性的
Hyprland 窗口放置规则并抑制焦点，不切换人类所在的工作区。外层窗口仍是
普通窗口，人类可以主动选择聚焦它。

协作小脑袋默认隐藏。在 `~/.config/agentseat/config.toml` 中设置
`eye_hud = true` 可以让它自动出现，也可以为当前 AgentSeat 会话执行
`agentseat hud start`。观察、虚拟输入和焦点隔离都不依赖小脑袋。

## 语言

AgentSeat 默认跟随系统语言，目前提供完整的英文、简体中文和繁体中文
界面。持久设置写入 `~/.config/agentseat/config.toml`：

```toml
[general]
language = "zh-CN" # auto、en、zh-CN 或 zh-TW
```

单次命令可以使用 `agentseat --language zh-TW --help`，一个进程树可以设置
`AGENTSEAT_LANGUAGE`。优先级依次是命令行覆盖、环境变量覆盖、配置文件、
系统 locale。不支持的语言回退到英文。不同语言下的 JSON 字段名、枚举值、
RPC 方法和错误码始终保持稳定。

## 数据与隐私

正常运行期间，源码和安装目录保持只读。执行 `agentseat paths` 可以查看
实际生效的 XDG 路径：

```text
~/.config/agentseat/config.toml   可选的用户配置
~/.local/share/agentseat/         持久应用数据
~/.local/state/agentseat/logs/    私有日志
~/.local/state/agentseat/captures 显式观察生成的图像
~/.cache/agentseat/               可随时丢弃的缓存
$XDG_RUNTIME_DIR/agentseat/       套接字、PID 文件和当前状态
```

聊天消息只存在于内存，守护进程退出后即消失。只有显式执行 `observe` 才会
写入截图。目录权限为 0700，私有文件权限为 0600。详见
[docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md)。

无须修改安装目录即可自定义默认值：

```sh
mkdir -p ~/.config/agentseat
cp config/config.toml.example ~/.config/agentseat/config.toml
agentseat config
```

## 架构与限制

Python 命令是短生命周期控制器。`agentseatd` 持有控制套接字和标准 Wayland
虚拟输入对象；精简后的 Cage 0.3.1 持有私有应用树和 XWayland 服务器。
观察功能通过标准 `wlr-screencopy` 读取私有输出，因此不会截取或移动宿主
桌面。

需要主动启用的协作小脑袋是私有微宿主表面。封装窗口内的人类输入拥有
优先权，并会暂时暂停 Agent 输入注入；Agent 的焦点变化只发生在私有应用
树内。

便捷命令 `start onlyoffice` 与 `run` 使用同一条原生 Wayland 通道。旧的
私有 X11 粘贴路径只用于兼容早期运行状态，不属于通用核心协议。

## 测试

离线测试不会打开 GUI：

```sh
pytest -q tests/test_controller.py
```

实时测试套件会打开并关闭真实应用，请在未使用的工作区运行：

```sh
python tests/live_generic_matrix.py
python tests/live_software_matrix.py
```

它们验证原生 Wayland 和私有 X11 输入、Unicode 文本、子窗口授权、观察、
清理，以及宿主焦点和指针隔离。通用矩阵默认使用宿主工作区 8；可以设置
`AGENTSEAT_TEST_WORKSPACE=N` 选择另一个空闲工作区。

## 许可证与来源

AgentSeat 采用 MIT 许可证。修改后的 Cage 微宿主继续保留其上游 MIT
许可证，来源信息位于 `vendor/cage`。详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和
[vendor/cage/UPSTREAM.md](vendor/cage/UPSTREAM.md)。
