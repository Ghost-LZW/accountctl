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
