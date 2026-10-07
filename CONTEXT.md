# Account Workspaces

以配置管理多个网站账号，每个账号绑定一个稳定的 Camoufox 浏览器身份。无需为新账号复制或修改脚本。

## Language

**账号（Account）**：
带稳定 ID、网站地址、标签和可选凭据引用的配置实体；不是某个浏览器进程或某个 Google 专用登录流程。
_Avoid_: 一个脚本、一个密码

**账号目录（Catalog）**：
一个 TOML 文件内的账号集合及默认配置，负责继承、验证与路径解析。

**浏览器环境（Profile）**：
一个账号独占的持久化目录，包含固定指纹及 Cookies/localStorage。账号之间不可共享。
_Avoid_: 隐身窗口、临时 context

**固化指纹（Frozen Fingerprint）**：
首次配置完成后的 Camoufox 生效参数与运行时版本绑定，包含 BrowserForge 原始输入之外的 Canvas、字体种子和 WebGL 参数。
_Avoid_: 仅 fingerprint.json、每次随机防指纹

**钉定浏览器（Pinned Browser）**：
固化指纹所绑定的那一个 Camoufox 浏览器构建（`135.0.1-beta.24`），不是"当前受支持的最新版"。上游有更新的构建，而 Camoufox 默认解析成第一个受支持的版本，因此这个钉定是 Frozen Fingerprint 能成立的前提。
_Avoid_: 最新版浏览器、随 fetch 漂移

**会话（Session）**：
某个账号当前正在运行的浏览器和管理进程，整个生命周期独占 Profile。

**凭据引用（Credential Reference）**：
指向 1Password 字段或环境变量的定位符，不是密码本身。账号导入只记录引用，代理使用时才按需解析。

## Modules

- `config`：账号目录 Module；调用方通过加载、选择和新增 Interface 使用继承与原子写入能力。
- `fingerprint`：固化指纹 Module；将所有随机选择和版本校验集中，恢复时不重新生成。
- `lifecycle` / `worker`：会话 Module 的前台 Interface 与后台 Implementation，负责锁、启动确认和协作关闭。
- `onepassword`：凭据 Module；元数据导入不越过秘密读取 Seam，环境变量和 1Password 是引用解析的两个 Adapter。
- `cli`：命令到上述 Interface 的映射，不承载浏览器内部逻辑。
- `browser_patch`：钉定浏览器 Module；把版本约束打进 venv 内的 camoufox（只作用于浏览器下载器），让 GeoIP 改走不限流的直链，并在系统回收缓存后从本地副本还原浏览器。由 `cli` 与 `worker` 在每次运行时调用，自动生效。

## Constraints

**只允许存在一个 camoufox。** 浏览器安装目录（`~/Library/Caches/camoufox`）由 `platformdirs` 决定，**所有** camoufox 安装共享同一个路径，无论装在哪个解释器下。camoufox 0.5.x 在 `camoufox_path()` 开头会无条件 `rmtree` 该目录（仅凭 `.0.5_FLAG` 是否存在判断），发生在检查参数之前，因此 `fingerprint.installed_executable()` 里的 `download_if_missing=False` 拦不住它。一旦被触发，钉定的浏览器构建被删除、重新下载成新版本，所有账号环境因运行时不符而拒绝启动。

2026-09-17 已因此卸载 anaconda 下手动安装的 `cloverlabs-camoufox` 0.5.5（一个 fork，import 名同为 `camoufox`，故 `version("camoufox")` 查不到它）。当时 anaconda 在 `PATH` 上优先于本项目 venv，未激活 venv 时裸敲 `python` 或 `camoufox` 命中的都是它。**不要在本机其他解释器上再安装任何 camoufox 发行版**；只用 `venv/bin/python`。

**跨 OS 伪装的账号需要宿主机装有对应字体。** camoufox 按伪装的 OS 过滤可用字体：`claude-mi` 声称 Windows，于是只放行微软雅黑 / SimSun 等 Windows 字体名。camoufox 虽自带 `msyh.ttc`，但只按名字放行、不真正加载它，宿主机没装就渲染成 .notdef 方块（中文全变空心方框）。这是 camoufox 的固有行为，原生启动路径同样如此，`gfx.bundled-fonts.activate` 无效。

2026-09-17 处理方式：从 camoufox 自带字体目录复制 `msyh*.ttc` 到 `~/Library/Fonts/`。选它而非改指纹，是因为改伪装 OS 会导致该账号需重建环境、重新登录。安装字体不影响固化指纹，无需重登。
