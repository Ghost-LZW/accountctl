# Account Workspaces

多账号浏览器管理仓库，每个账号一个独立进程、持久化目录和固化身份；配置与启动分离，不再复制脚本管理账号。

两种引擎按账号选择，取舍是“身份彼此不同”还是“单个身份最像正常人”：

- `camoufox`（默认）：canvas、WebGL、字体、硬件参数逐账号随机固化，还能钉死一个构建版本。账号多、彼此不能相关时用它。
- `chrome`：本机真实 Chrome，指纹就是普通 Chrome 的样子，最容易过 hCaptcha / Cloudflare 的自动判定。**同一台机器的 canvas、WebGL、GPU、TLS 是共享的**，账号多了会相关起来；用 `--gpu`、`--scale`、`--canvas-noise` 和不同的窗口尺寸拉开距离，规模再大就该换回 camoufox 或商业指纹浏览器。

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

`accountctl probe <id>` 用临时 profile 起一次浏览器，输出可比的指纹自检（canvas、WebGL、时区、语言、webdriver、DNT/GPC），不碰登录态；加 `--engine` 可以横向比另一个引擎，加 `--login` 会用 `probe@example.com` 走到验证码页，量 Cloudflare 和 hCaptcha 是不是要人解——不用消耗真实邮箱。

`doctor` 会按账号真实发起一次出口探测（因此会按需读取 `proxy_ref`，但不打印密码），并检查运行时、指纹与重复指纹。

`add` 也可直接指定窗口、GeoIP 与代理来源，不必手工编辑 TOML：

```bash
accountctl add claude03 --label "Claude · 03" --provider claude --tag claude \
  --url https://claude.ai/ --window 1280x800 --geoip --proxy-from-account claude02
```

`--proxy`、`--proxy-ref`、`--proxy-from-account` 三选一。位置默认跟随出口；`--geoip` 是 `--egress auto` 的兼容别名，可与 `--locale/--timezone` 同时使用（后者变成受校验的固定值）。`--egress`、`--webrtc`、`--probe-url` 见 [浏览器必须与出口一致](#浏览器必须与出口一致)。

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
4. 固定窗口。随机参数始终冻结；位置信号（时区、语言、坐标、WebRTC IP）则**每次启动前从代理出口重新推导**，见 [浏览器必须与出口一致](#浏览器必须与出口一致)。改语言/时区不再需要重建环境。
5. 记录 Camoufox、BrowserForge、Playwright 版本及浏览器二进制 SHA-256。运行时变更会拒绝启动；不要盲目更新依赖或重新 fetch。

浏览器版本由 `browser_patch` 模块钉在 `152.0.4-beta.30`。上游已发布更新的版本，而 Camoufox 默认会自动取"第一个受支持的"版本，一旦升级就与上面第 5 点固化的运行时不符，所有账号环境都会拒绝启动（`CamoufoxFetcher.install()` 还会先删掉原有安装再下载）。同一个模块还把 GeoIP 数据库改为走 GitHub 的 `latest/download` 直链：`GeoLite2-City.mmdb` 存在 `site-packages` 而不随 wheel 发布，重建 venv 会连带删掉，而原本的下载路径走限流的 GitHub API（共享出口 IP 常被耗尽）。

这些修补由 `accountctl` 在每次运行时自动完成，**不需要你记住任何命令**：补丁改在不入库的 `venv/` 内，重建环境会丢失；浏览器装在 `~/Library/Caches`，可能被系统回收。两者都会在下次运行时自动还原（浏览器从 `~/Library/Application Support/account-workspaces/browser/` 下的本地副本恢复，不重新下载，因而不会换成别的构建）。需要手动检查时用 `python -m account_workspaces.browser_patch --check`。

这是对管理范围内指纹配置的稳定性保证，**不保证网站看到的所有特征永久不变或绕过风控**：出口 IP、硬件、系统字体、浏览器安全升级、网站采集算法等仍可能影响结果。换设备前先备份并验证。不同账号可有相同分辨率或 UA，但完整配置及随机种子不同。

新账号默认启用 WebRTC，并把对外 IP 伪装成出口地址（`webrtc = "spoof"`）；可用 `block` 完全关闭、`keep` 不干预。旧 profile 的兼容模式默认 `keep`，保留原有 WebRTC/默认扩展行为。不自动装载 Camoufox 默认扩展。不要直接改固化文件或升级旧环境扩展。

版本升级不是永久禁止：先备份完整 profile，使用新测试账号验证新版本，再规划迁移；当前版本故意不提供“一键重置指纹”，避免误改已登录身份。

## 浏览器必须与出口一致

上游能读到的位置信号必须与**代理的实际出口 IP** 一致。美国出口配 `Asia/Shanghai` 时区是明显的矛盾，比不配代理更可疑。为此每次 `start` 都按下面的顺序执行，**位置不参与指纹冻结**：

1. 用浏览器同一个代理、并强制忽略本机 `NO_PROXY`，向两个独立端点查询出口 IP。
2. 两个端点必须给出同一个 IP，否则视为分流不一致并拒绝启动。
3. 用本地 MaxMind 数据库把这个 IP 解析成国家、城市、时区、坐标；语言取该国最常用的语言（同一国家内保持已选项不变）。
4. 把时区同时写进 `TZ` 和 Camoufox 配置，语言写进 `CAMOU_CONFIG`、`LANG/LC_ALL`、`intl.accept_languages`，坐标写进 geolocation。
5. 启动后在第一张真实页面回读 `Intl` 时区和 `navigator.language`，不符则关闭并报错。

同时关闭宿主泄漏渠道：`geo.provider.use_corelocation/use_geoclue`、`javascript.use_us_english_locale`、`intl.regional_prefs.use_os_locales` 全关；`network.trr.mode=0` 且代理侧解析 DNS，避免 DoH 绕开代理；WebRTC 默认把对外 IP 换成出口 IP，并关闭 IPv6 候选。

```toml
[accounts.browser]
proxy_ref = "op://Private/l4dev-clash-proxy/proxy"
egress = "auto"          # 默认。出口换国家时跟随
# egress = "strict"      # 换国家则拒绝启动，身份不跳变
# egress = "warn"        # 回读不符只记录，不关闭
# egress = "off"         # 退回旧行为：只用写死的 locale/timezone
# webrtc = "block"       # 或不想要 WebRTC 时
```

`locale` / `timezone` 仍可写死，但会被校验：时区与出口时区相差超过 3 小时、或语言不是出口国前两位语言时拒绝启动。`en-US` + `America/New_York` 出口美国当然可以（同国的跨时区也放过）；`zh-CN` 出口美国就会被拒。

**分流代理（Clash/mihomo `mode: rule`）**：探测域名若被规则送到别的节点或直连，第 2 步会报“出口不同”。把 `probe_url` 指向与你目标站点同规则的地址即可（例如规则里 `ipinfo.io` 与 `claude.ai` 同为 US-Proxy 时，`probe_url = "https://ipinfo.io/json"`）：

```toml
probe_url = "https://ipinfo.io/json"
```

确认当前出口：`accountctl doctor` 会按账号执行完整探测并打印国家/时区；`accountctl status` 显示上次启动的出口与 `（未验证）` 标记。机器自己的 `curl` 结果不可信——它走的是另一个进程的路由。

注意 Camoufox 0.4.11 的 `timezone` **配置项本身不生效**（`locale` 生效），实测 JS 仍报告宿主时区；本仓库额外注入 `TZ`。升级 Camoufox 后需重新验证。地理坐标由配置注入，但 `getCurrentPosition()` 在个别构建上会阻塞页面事件循环，所以启动回读只校验时区和语言。

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

结构与领域术语见 [CONTEXT.md](CONTEXT.md)，设计取舍见 [docs/adr/](docs/adr/)（指纹冻结、出口推导）。
