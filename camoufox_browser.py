import argparse
import json
import os
import urllib.parse
from pathlib import Path

try:
    from browserforge.fingerprints import FingerprintGenerator, Fingerprint
    from camoufox.sync_api import Camoufox
except ImportError:
    print("[-] 缺少依赖库。请先执行：pip install 'camoufox[geoip]' browserforge")
    exit(1)

LOCAL_NO_PROXY = (
    "localhost",
    "127.0.0.1",
    "127.0.0.0/8",
    "::1",
    "0.0.0.0",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    ".local",
)


def merge_no_proxy(existing: str, additions: tuple[str, ...]) -> str:
    entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
    seen = {entry.lower() for entry in entries}
    for entry in additions:
        if entry.lower() not in seen:
            entries.append(entry)
            seen.add(entry.lower())
    return ",".join(entries)


def ensure_local_no_proxy() -> str:
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
    merged = merge_no_proxy(existing, LOCAL_NO_PROXY)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
    return merged


def parse_proxy(proxy_str: str) -> dict:
    """
    解析代理字符串。
    支持格式: http://user:pass@127.0.0.1:8080 或 socks5://ip:port
    """
    parsed = urllib.parse.urlparse(proxy_str)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"http://{proxy_str}")
        
    proxy_dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    
    if parsed.username:
        proxy_dict["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        proxy_dict["password"] = urllib.parse.unquote(parsed.password)
        
    return proxy_dict


def build_proxy_config(proxy_str: str) -> tuple[dict, str]:
    proxy_config = parse_proxy(proxy_str)
    no_proxy = ensure_local_no_proxy()
    return proxy_config, no_proxy

def dict_to_dataclass(cls, data):
    """
    将 JSON 字典安全转换为指定的 Dataclass（处理嵌套情况）。
    BrowserForge 的 Fingerprint 是嵌套的数据类，直接传入 kwargs 会导致内层依然是字典。
    """
    from dataclasses import fields, is_dataclass
    if not is_dataclass(cls):
        return data
    try:
        fieldtypes = {f.name: f.type for f in fields(cls)}
        return cls(**{k: dict_to_dataclass(fieldtypes[k], v) if isinstance(v, dict) else v for k, v in data.items()})
    except Exception:
        return data

def main():
    parser = argparse.ArgumentParser(description="Camoufox 浏览器管理器 - 基于独立配置文件夹管理")
    
    # 互斥组：新建环境 vs 恢复环境
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-n", "--new-profile", type=str, metavar="DIR",
                       help="创建一个新环境（配置文件夹），生成随机指纹并初始化会话缓存")
    group.add_argument("-r", "--restore-profile", type=str, metavar="DIR",
                       help="从指定的环境（配置文件夹）恢复浏览器指纹与账号状态")
    
    parser.add_argument("-p", "--proxy", type=str, metavar="URL",
                        help="设置代理服务器 (例如: http://user:password@ip:port)")
    parser.add_argument("--geoip", action="store_true",
                        help="启用后，自动根据代理 IP 伪造地理位置、时区和语言 (推荐配合代理使用)")
    parser.add_argument("--headless", action="store_true",
                        help="后台无头模式运行（不显示浏览器界面）")
    parser.add_argument("-w", "--window", type=str, metavar="WIDTHxHEIGHT",
                        help="强制指定窗口和指纹屏幕分辨率 (例如: 1280x720)")

    args = parser.parse_args()

    # 确定目标配置文件夹路径
    profile_dir = Path(args.new_profile) if args.new_profile else Path(args.restore_profile)
    fp_path = profile_dir / "fingerprint.json"
    meta_path = profile_dir / "meta.json"
    user_data_dir = profile_dir / "user_data"  # 默认在文件夹内创建缓存目录

    fp = None
    saved_meta = {}

    # 1. 初始化新环境
    if args.new_profile:
        print(f"[*] 正在初始化新浏览器环境: {profile_dir.absolute()}")
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        if not args.window:
            # 如果未指定窗口大小，强制赋予一个合理的大小以避免黑屏
            args.window = "1920x1080"
            
        # 保存代理、窗口大小等元数据
        saved_meta = {
            "proxy": args.proxy,
            "window": args.window,
            "geoip": args.geoip
        }
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(saved_meta, f, indent=4)

    # 2. 恢复旧环境
    if args.restore_profile:
        if not fp_path.exists():
            print(f"[-] 错误: 在环境目录 {profile_dir} 中找不到指纹文件 (fingerprint.json)")
            return
            
        print(f"[*] 正在从 {profile_dir.absolute()} 恢复浏览器环境...")
        
        # 读取元数据（例如之前保存的代理配置）
        if meta_path.exists():
            with open(meta_path, 'r', encoding='utf-8') as f:
                saved_meta = json.load(f)

        # 尝试从元数据中恢复窗口分辨率
        if not args.window:
            if saved_meta.get("window"):
                args.window = saved_meta.get("window")
                
        # 如果命令行没有指定代理，但之前配置过代理，那么自动使用配置的代理
        if not args.proxy and saved_meta.get("proxy"):
            args.proxy = saved_meta.get("proxy")
            
        # 恢复 geoip 配置状态
        if not args.geoip and saved_meta.get("geoip"):
            args.geoip = saved_meta.get("geoip")
            
        from browserforge.fingerprints import Fingerprint

    # 3. 代理配置
    proxy_config = None
    if args.proxy:
        proxy_config, no_proxy = build_proxy_config(args.proxy)
        print(f"[*] 已配置代理: {proxy_config['server']}")
        print(f"[*] 本地/内网地址将绕过代理: {no_proxy}")

    print("[*] 正在启动防指纹浏览器...")
    
    # 4. 浏览器启动参数
    camoufox_kwargs = {
        "proxy": proxy_config,
        "geoip": args.geoip,
        "headless": args.headless,
        "i_know_what_im_doing": True,  # 取消自己传自定义指纹的控制台警告
        # 强制开启持久化上下文，并将目录指定为我们配置文件夹下的 user_data 目录
        "persistent_context": True,
        "user_data_dir": str(user_data_dir.absolute()),
    }
    
    if args.new_profile:
        # 让 generator 生成一遍
        from browserforge.fingerprints import FingerprintGenerator
        if args.window:
            try:
                width, height = map(int, args.window.lower().split("x"))
                from browserforge.fingerprints import Screen
                # 将分辨率约束在生成器级别（适当放宽限制，防止因为特定分辨率导致无匹配 header 生成报错）
                gen = FingerprintGenerator(browser="firefox", screen=Screen(min_width=width, max_width=width+500, min_height=height, max_height=height+500))
            except Exception as e:
                print(f"[-] 分辨率格式错误: {e}")
                gen = FingerprintGenerator(browser="firefox")
        else:
            gen = FingerprintGenerator(browser="firefox")
            
        fp = gen.generate()
        camoufox_kwargs["fingerprint"] = fp
        
        # 将生成的指纹基础数据保存备用
        with open(fp_path, 'w', encoding='utf-8') as f:
            f.write(fp.dumps())
        print(f"[+] 新指纹已由 Generator 生成并保存至: {fp_path}")
    
    if args.restore_profile:
        if fp_path.exists():
            with open(fp_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 由于指纹文件里存放的是 json，我们通过 dict_to_dataclass 转回 Fingerprint 对象
            camoufox_kwargs["fingerprint"] = dict_to_dataclass(Fingerprint, data)
            print("[+] 已加载并应用上次的底层指纹配置！")
            
    if args.window:
        try:
            width, height = map(int, args.window.lower().split("x"))
            
            # 默认情况下赋予全尺寸
            camoufox_kwargs["window"] = (width, height)
            
            # 为了防止它读取 user_data 里面 xulstore.json 记忆的旧窗口大小，
            # 我们通过 Firefox 的用户配置强制限制尺寸
            if "firefox_user_prefs" not in camoufox_kwargs:
                camoufox_kwargs["firefox_user_prefs"] = {}
            # 设置初始内外尺寸，最大宽度强制设置，避免恢复配置文件干扰
            camoufox_kwargs["firefox_user_prefs"]["privacy.window.maxInnerWidth"] = width
            camoufox_kwargs["firefox_user_prefs"]["privacy.window.maxInnerHeight"] = height
            camoufox_kwargs["firefox_user_prefs"]["privacy.window.maxOuterWidth"] = width
            camoufox_kwargs["firefox_user_prefs"]["privacy.window.maxOuterHeight"] = height
            
            # 允许脚本控制窗口大小，同时关闭可能导致黑边的 letterboxing 防指纹策略
            camoufox_kwargs["firefox_user_prefs"]["dom.disable_window_move_resize"] = False
            camoufox_kwargs["firefox_user_prefs"]["privacy.resistFingerprinting.letterboxing"] = False
            
            # 如果这是从旧配置启动，我们需要告诉它重置 xulstore 相关的全部记忆
            if args.restore_profile:
                camoufox_kwargs["firefox_user_prefs"]["browser.sessionstore.restore_window_behavior"] = 0
                camoufox_kwargs["firefox_user_prefs"]["browser.sessionstore.restore_on_demand"] = False
                camoufox_kwargs["firefox_user_prefs"]["browser.sessionstore.resume_from_crash"] = False
                camoufox_kwargs["firefox_user_prefs"]["browser.startup.page"] = 1
                camoufox_kwargs["firefox_user_prefs"]["toolkit.legacyUserProfileCustomizations.stylesheets"] = True
                
                # 强制覆盖 xulstore.json 中记录的旧窗口配置
                xulstore_path = user_data_dir / "xulstore.json"
                if xulstore_path.exists():
                    try:
                        xul_data = json.loads(xulstore_path.read_text(encoding="utf-8"))
                        if "chrome://browser/content/browser.xhtml" in xul_data:
                            if "main-window" in xul_data["chrome://browser/content/browser.xhtml"]:
                                xul_data["chrome://browser/content/browser.xhtml"]["main-window"]["width"] = str(width)
                                xul_data["chrome://browser/content/browser.xhtml"]["main-window"]["height"] = str(height)
                                # 也可以移除位置记忆让它居中，不过重点是宽和高
                                xulstore_path.write_text(json.dumps(xul_data), encoding="utf-8")
                    except Exception:
                        try:
                            xulstore_path.unlink()
                        except Exception:
                            pass
            
            # 通过 Playwright viewport 属性传递初始大小
            camoufox_kwargs["viewport"] = {"width": width, "height": height}
            # Firefox 的 -width 和 -height 已经被弃用了或者在这个版本里不兼容，因此不再通过 command line args 传递
            
            # 由于在恢复旧指纹时，原指纹中的分辨率可能是别的值
            # 为了防止被旧指纹带偏导致窗体过大或过小，我们直接通过 config 注入强制覆盖屏幕参数
            if "config" not in camoufox_kwargs:
                camoufox_kwargs["config"] = {}
            camoufox_kwargs["config"]["screen.width"] = width
            camoufox_kwargs["config"]["screen.height"] = height
            camoufox_kwargs["config"]["screen.availWidth"] = width
            camoufox_kwargs["config"]["screen.availHeight"] = height
            camoufox_kwargs["config"]["window.innerWidth"] = width
            camoufox_kwargs["config"]["window.innerHeight"] = height - 100
            camoufox_kwargs["config"]["window.outerWidth"] = width
            camoufox_kwargs["config"]["window.outerHeight"] = height
        except Exception:
            pass

    # 启动浏览器
    with Camoufox(**camoufox_kwargs) as browser:
        
        # 当使用 persistent_context 启动时，浏览器默认会自动存在一个标签页
        page = browser.pages[0] if browser.pages else browser.new_page()
        
        if args.window:
            try:
                width, height = map(int, args.window.lower().split("x"))
                
                page.set_viewport_size({"width": width, "height": height})
                # 显式调整浏览器外壳窗口的大小
                page.wait_for_timeout(500)
                page.evaluate(f"window.resizeTo({width}, {height})")
                page.wait_for_timeout(500)
                
            except Exception:
                pass
        
        # 在新建环境的第一次启动时，保存被 camoufox 和 playwright 融合调整后的最终指纹
        if args.new_profile:
            pass
                
        test_url = "https://abrahamjuliot.github.io/creepjs/"
        print(f"[*] 正在访问测试页面: {test_url}")
        
        try:
            page.goto(test_url, timeout=60000)
            print("[+] 页面加载成功。当前缓存和 Cookies 将会自动保存在 user_data 目录中。")
            
            if not args.headless:
                input(">>> 按下 Enter 键以安全关闭浏览器并保存数据...")
            else:
                print("[+] 无头模式运行完毕，浏览器已自动退出。")
                
        except Exception as e:
            print(f"[-] 页面访问出现异常: {e}")

if __name__ == "__main__":
    main()
