# Account Workspaces

只支持 **Camoufox** 的多账号浏览器管理仓库。一个账号一个独立进程、持久化目录和固化指纹；配置与启动分离，不再复制脚本管理账号。

## 快速开始

需要 Python 3.11+；优先支持 macOS / Linux。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m camoufox fetch
accountctl init
accountctl add google-personal --provider google --tag google \
  --url https://accounts.google.com/
accountctl start google-personal
accountctl list
accountctl stop google-personal
```

当前目录已经有 `venv` 时，可以 `source venv/bin/activate`，不必另建环境。
`start` 启动后台管理进程，退出终端不会关闭浏览器。首次在浏览器中手动登录；关闭窗口或 `stop` 后登录状态保留。

```bash
accountctl start --tag google  # 启动标签匹配的账号
accountctl start account-a account-b
accountctl start account-a --no-urls  # 本次只开空白页，不打开配置的网址
accountctl status --all
accountctl stop --all
accountctl validate
accountctl doctor
accountctl --config /absolute/path/accounts.toml list
```

标签可重复，多个标签取交集；批量启动部分失败会返回非零状态，其他已成功启动的账号保持运行。`--headless` 用于测试，不是自动登录模式。

`--no-urls` 只影响本次启动，不修改 `accounts.toml`；想长期不打开网址，把账号的 `urls` 删掉即可继承 `[defaults]`（默认 `about:blank`）。`urls` 不参与指纹校验，改动它不需要重建环境。

`add` 也可直接指定窗口、GeoIP 与代理来源，不必手工编辑 TOML：

```bash
accountctl add claude03 --label "Claude · 03" --provider claude --tag claude \
  --url https://claude.ai/ --window 1280x800 --geoip --proxy-from-account claude02
```

`--geoip` 与 `--locale/--timezone` 互斥；`--proxy`、`--proxy-ref`、`--proxy-from-account` 三选一。

## 从 1Password 批量配置

安装官方 [1Password CLI](https://developer.1password.com/docs/cli/get-started/)，在桌面 App 中启用 CLI 集成并解锁。

```bash
# 先预览：仅列出 Login 条目的元数据，不读取 username/password
accountctl import-1password --vault Private --domain google.com --prefix google

# 确认后写入本机账号目录
accountctl import-1password --vault Private --domain google.com --prefix google --apply
accountctl start --tag google.com
```

支持 `--tag` 筛选 1Password 标签；多个 1Password 账号使用 `--op-account`。ID 使用条目完整 ID，重复导入跳过同一条目引用，不覆盖手动配置。域名严格匹配该域及子域，`google.com.evil.example` 不会匹配。

每条账号只保存 `op://vault-id/item-id/username`、`password` 引用；不导出整个保险库，也不读取密码。条目必须带匹配的网站 URL 才能通过 `--domain` 筛选；没有网站 URL 的条目可按 `--tag` 导入，之后自行配置启动地址。

**本版本不自动填密码、点击登录或处理 MFA。** 账号凭据引用用于关联身份，启动时不解析 `username_ref/password_ref`；你在独立浏览器中完成首次登录。需要认证的代理才会在启动时按需解析 `proxy_ref`，不写入状态文件或日志。

## 配置任意账号

参考 [accounts.example.toml](accounts.example.toml)。账号不是 Google 专用概念，新增任意网站不需改代码：

```toml
[[accounts]]
id = "my-service"
label = "我的服务"
provider = "custom"
tags = ["work"]
urls = ["https://example.com/"]

[accounts.credentials]
username_ref = "op://Private/my-service/username"
password_ref = "op://Private/my-service/password"

[accounts.browser]
window = [1440, 900]
locale = "en-US"
timezone = "UTC"
proxy_ref = "env://SERVICE_PROXY"
```

默认配置从当前目录的 `accounts.toml` 读取，也可设置 `ACCOUNT_WORKSPACES_CONFIG`。相对路径始终相对配置文件，不相对启动目录。新增命令原子更新并保留 TOML 注释；未知字段、重复 ID、共享 profile、明文密码都会被拒绝。

`enabled = false` 禁止启动，但仍可查看和关闭运行中的账号。删除账号配置前先关闭对应浏览器；删除配置不会删除 profile，可恢复配置重新接入。不要手动修改运行中账号的 ID、profile 或 state_dir。

## 稳定且不同的指纹

原脚本只保存 BrowserForge 输入，而 Camoufox 每次生成启动配置时仍随机选择字体间距种子、Canvas 和 WebGL 等参数。现在：

1. 首次为每个账号单独生成 `fingerprint.json`，并保存完整生效配置到 `workspace.json`。
2. 之后直接复用生效配置，跳过随机生成；校验输入和生效配置哈希。
3. 绑定账号 ID，拒绝将已有 profile 复制给另一账号。不同账号的随机参数独立生成，`doctor` 检查重复指纹。
4. 固定窗口、语言和时区。`geoip = true` 时首次通过账号代理解析地理位置/时区/语言并固化；后续不重新采样。更改这些配置会拒绝启动，不会悄悄重建。
5. 记录 Camoufox、BrowserForge、Playwright 版本及浏览器二进制 SHA-256。运行时变更会拒绝启动；不要盲目更新依赖或重新 fetch。

浏览器版本由 `browser_patch` 模块钉在 `135.0.1-beta.24`。上游已发布更新的版本，而 Camoufox 默认会自动取"第一个受支持的"版本，一旦升级就与上面第 5 点固化的运行时不符，所有账号环境都会拒绝启动（`CamoufoxFetcher.install()` 还会先删掉原有安装再下载）。同一个模块还把 GeoIP 数据库改为走 GitHub 的 `latest/download` 直链：`GeoLite2-City.mmdb` 存在 `site-packages` 而不随 wheel 发布，重建 venv 会连带删掉，而原本的下载路径走限流的 GitHub API（共享出口 IP 常被耗尽）。

这些修补由 `accountctl` 在每次运行时自动完成，**不需要你记住任何命令**：补丁改在不入库的 `venv/` 内，重建环境会丢失；浏览器装在 `~/Library/Caches`，可能被系统回收。两者都会在下次运行时自动还原（浏览器从 `~/Library/Application Support/account-workspaces/browser/` 下的本地副本恢复，不重新下载，因而不会换成别的构建）。需要手动检查时用 `python -m account_workspaces.browser_patch --check`。

这是对管理范围内指纹配置的稳定性保证，**不保证网站看到的所有特征永久不变或绕过风控**：出口 IP、硬件、系统字体、浏览器安全升级、网站采集算法等仍可能影响结果。换设备前先备份并验证。不同账号可有相同分辨率或 UA，但完整配置及随机种子不同。

新账号 WebRTC 默认关闭以避免真实网络信息泄露，视频通话功能因此受限；不自动装载 Camoufox 默认扩展。旧 profile 的兼容模式保留原 WebRTC/默认扩展行为，GeoIP 时保留其 WebRTC IP 设置。不要直接改固化文件或升级旧环境扩展。

版本升级不是永久禁止：先备份完整 profile，使用新测试账号验证新版本，再规划迁移；当前版本故意不提供“一键重置指纹”，避免误改已登录身份。

## 接入现有 profiles/

原来的 `camoufox_browser.py` 和未提交修改保持不动，仅作为历史入口；新工程入口是 `accountctl`。不要同时用旧脚本打开同一个 profile。

```bash
accountctl adopt-profile old-google ./profiles/your-old-profile \
  --url https://accounts.google.com/
accountctl start old-google
```

登记不会搬动/删除旧文件，也不重写原始 `fingerprint.json`。复用原 `user_data`，从旧 `meta.json` 继承窗口、GeoIP 和代理。带密码的旧代理通过 `proxy_from_meta = true` 在运行时读取，**不将密码复制进 accounts.toml**；也可用 `--proxy-ref` 换成 1Password/环境变量引用。旧文件不会被擅自清理。

你现有的 `claude02` 命令对应：

```bash
accountctl adopt-profile claude02 ./profiles/claude02
accountctl start claude02
```

原 `meta.json` 已含 `1280x800`、`geoip=true` 与原认证代理，因此不必在命令行重复输入密码。兼容模式保留原窗口覆盖、viewport、Firefox prefs、BrowserForge 输入及代理。旧脚本入口也继续可用，但不要和新命令同时操作同一 profile；接入固化后建议统一使用新命令。

旧脚本从未保存的 Canvas/WebGL 参数无法恢复历史值；首次接入会固化这些参数，从此稳定。GeoIP 首次使用原代理查询；如果代理出口是动态的，查询结果可能与历史运行不同，不能保证恢复旧出口 IP 的地理指纹。固化后换代理也不会自动更改地理参数。

## 数据与安全

```text
accounts.toml                    # 本机账号配置（Git 忽略）
.account-workspaces/profiles/
  google-personal/
    fingerprint.json             # 原始 BrowserForge 输入
    workspace.json               # 完整指纹、哈希、版本绑定
    user_data/                   # Cookies / localStorage / 浏览器登录状态
    session.json                 # 进程状态，不含密码
    .accountctl.lock              # 跨进程独占锁
```

新状态目录权限为 `0700`，管理器写出的文件为 `0600`；**Cookies 依然等同敏感凭据**，Git 忽略不是加密。只在加密磁盘使用并加密备份整个 profile。1Password CLI 错误、浏览器启动参数和第三方异常原文不写入日志，避免泄漏秘密。

新配置的代理只支持无认证 URL 或 `op://` / `env://` 引用；旧兼容模式额外允许直接引用原 `meta.json`。旧文件中的明文密码仍需你自行迁移到 1Password，本工具不擅自覆盖。本地 Playwright 控制连接会合并 `NO_PROXY`；这不等于所有浏览器内网访问都会自动绕过上游代理。Chrome/Chromium、远程调试端口、跨账号共享 cookie、批量自动登录均不在范围内。

多个账号复用同一代理时，可设置 `proxy_from_account = "claude02"`（等价的命令行参数是 `accountctl add --proxy-from-account claude02`，`adopt-profile` 同名参数用于旧 `meta.json` 没有代理的情况）。
这只复用代理来源，不共享指纹、Cookies 或窗口设置，也不复制代理密码。
被引用的账号必须保留在配置中；循环引用、未知账号或未配置代理都会被拒绝。

## 开发与验证

```bash
pip install -e '.[dev]'
python -m pytest
ruff check src tests
ruff format --check src tests
python -m build

# 真实 Camoufox 测试：仅临时目录与本机页面，不碰现有账号
ACCOUNT_WORKSPACES_BROWSER_TESTS=1 python -m pytest -m browser -v

# 指定旧环境的兼容测试：只复制指纹，不复制/打开原 Cookies；
# 通过原代理执行 GeoIP 和一次 example.com 只读请求
ACCOUNT_WORKSPACES_LEGACY_TEST_PROFILE=profiles/claude02 \
  python -m pytest tests/test_legacy_compat.py -v
```

结构与领域术语见 [CONTEXT.md](CONTEXT.md)，设计取舍见 [docs/adr/0001-frozen-camoufox.md](docs/adr/0001-frozen-camoufox.md)。
