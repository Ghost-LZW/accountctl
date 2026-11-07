"""Derive every location signal from the proxy's real exit, on every launch.

A browser that reports ``Europe/Berlin`` time while its packets leave from
Miami is more suspicious than no proxy at all. Camoufox's ``geoip=True``
resolves the location once, at fingerprint creation, and does it by asking a
public IP service *through the proxy*. That is correct only when the proxy has
one global exit: under a rule-based proxy (mihomo ``mode: rule``) the probe
host can be routed to another node -- or direct -- than the sites the account
actually visits, so the frozen location describes a different machine.

This module therefore resolves the exit on **every start**, with the same
authenticated proxy the browser uses, and refuses to start when the result is
not trustworthy:

* the probe requests are pinned to the proxy (``trust_env`` is off, so a local
  ``NO_PROXY`` cannot quietly turn them into direct requests);
* with the default probes, two independent endpoints must agree on the exit
  IP, which exposes split routing (a configured ``probe_url`` replaces both,
  because it is the one known to share the target's route);
* the timezone and coordinates come from the local MaxMind database, not from
  whichever JSON endpoint happened to answer;
* an explicitly configured ``locale``/``timezone`` is verified against the
  exit instead of being trusted blindly.

``egress = "strict"`` additionally refuses to move to another country.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import WorkspaceError

IPINFO = "https://ipinfo.io/json"
IPAPI = "https://ipapi.co/json/"
DEFAULT_PROBES = (IPINFO, IPAPI)
STATE = "egress.json"
# Two responses are enough to expose split routing; a third only costs latency.
MAX_PROBES = 3
# A pinned timezone may sit within this many seconds of the exit's zone.
MAX_PIN_OFFSET = 3 * 60 * 60


@dataclass(frozen=True)
class Egress:
    """What the exit looks like to an upstream, and what the browser must echo."""

    ip: str
    country: str
    timezone: str
    latitude: float
    longitude: float
    locale: str
    accuracy: float = 20.0

    def state(self) -> dict:
        return {
            "ip": self.ip,
            "country": self.country,
            "timezone": self.timezone,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "locale": self.locale,
            "probed_at": datetime.now(tz=ZoneInfo(self.timezone)).isoformat(timespec="seconds"),
        }


def validate_probe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        raise WorkspaceError("probe_url 必须是 http(s) 地址，且不能包含凭据")
    return value


def requests_proxy(proxy: dict[str, str] | None) -> dict[str, str] | None:
    """Encode credentials for requests, which unlike Camoufox needs them in the URL."""
    if not proxy:
        return None
    user = proxy.get("username")
    password = proxy.get("password")
    auth = ""
    if user is not None:
        auth = quote(user, safe="")
        if password is not None:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    server = proxy["server"]
    scheme, _, rest = server.partition("://")
    return {"http": f"{scheme}://{auth}{rest}", "https": f"{scheme}://{auth}{rest}"}


def _get(url: str, proxy: dict[str, str] | None) -> str:
    import requests

    with warnings.catch_warnings():
        # The same MITM proxy certificate situation as Camoufox: verify=False.
        warnings.simplefilter("ignore")
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                url,
                proxies=requests_proxy(proxy),
                timeout=8,
                verify=False,
                headers={
                    "Accept": "application/json, text/plain",
                    "User-Agent": "accountctl/1",
                },
            )
            response.raise_for_status()
            return response.text
        finally:
            session.close()


def _payload(text: str | dict) -> dict:
    """JSON (ipinfo/ipapi) or Cloudflare's ``key=value`` trace format."""
    if isinstance(text, dict):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        data = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
        if "loc" in data:  # trace uses `loc` for the country
            data.setdefault("country", data["loc"])
        if not data:  # a bare IP echo such as api.ipify.org
            data = {"ip": text.strip()}
    if not isinstance(data, dict):
        raise ValueError
    return data


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError
    return float(value)


def parse_payload(data: dict) -> dict:
    """Accept ipinfo's shape, ipapi's shape, Cloudflare trace, and a small custom shape."""
    from camoufox.ip import validate_ip

    ip = data.get("ip")
    try:
        validate_ip(ip)
    except Exception:  # noqa: BLE001 - camoufox raises its own InvalidIP
        raise ValueError from None
    country = data.get("country_code") or data.get("country")
    if country is not None and (not isinstance(country, str) or len(country.strip()) != 2):
        raise ValueError
    result = {"ip": ip, "country": country.strip().upper() if country else None}
    loc = data.get("loc")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if latitude is None and isinstance(loc, str) and "," in loc:
        latitude, longitude = loc.split(",", 1)
    # Coordinates are optional: the local MaxMind database is authoritative.
    result["latitude"] = None if latitude in (None, "") else _number(latitude)
    result["longitude"] = None if longitude in (None, "") else _number(longitude)
    if result["latitude"] is not None and (
        not -90 <= result["latitude"] <= 90 or not -180 <= result["longitude"] <= 180
    ):
        raise ValueError
    timezone = data.get("timezone")
    result["timezone"] = timezone if isinstance(timezone, str) else None
    return result


def _probe_many(
    urls: tuple[str, ...], proxy: dict[str, str] | None, *, custom: bool = False
) -> list[dict]:
    answers, errors = [], []
    for url in urls[:MAX_PROBES]:
        try:
            answers.append(parse_payload(_payload(_get(url, proxy))))
        except Exception as error:  # noqa: BLE001 - reported together below
            errors.append(f"{url}: {type(error).__name__}")
    if not answers:
        via = "代理" if proxy else "直连出口"
        hint = (
            "probe_url 无法经代理访问；确认它与目标站点同规则、且节点可用。"
            if custom
            else '可用 egress="off" 关闭，或设置 probe_url 指向被同一规则覆盖的地址。'
        )
        raise WorkspaceError(
            f"无法确认{via} IP（探测端点均失败）；拒绝在位置未知时启动。{hint}"
            f" [{'; '.join(errors)}]"
        )
    ips = {answer["ip"] for answer in answers}
    if len(ips) > 1:
        raise WorkspaceError(
            "代理分流导致各探测端点出口不同："
            + "、".join(sorted(ips))
            + "；浏览器位置会与部分上游不一致。请让 probe_url 走与目标站点相同的规则"
        )
    return answers


def _locate(ip: str) -> tuple[str, float, float, str] | None:
    """Look the exit up in the local MaxMind database; none means service data."""
    from camoufox.locale import get_geolocation

    try:
        location = get_geolocation(ip)
    except Exception:  # noqa: BLE001 - missing DB/no entry: fall back to the response
        return None
    country = (location.locale.region or "").upper()
    return location.timezone, location.latitude, location.longitude, country


def _spoken(country: str, locale: str) -> bool:
    """Plausible means the language's own region, or one of the country's top two.

    A US exit reporting ``zh-CN`` is technically possible, but it is a routine
    mismatch signal, so a pinned locale must be more than merely present.
    """
    from camoufox.locale import SELECTOR

    language, region = locale.split("-", 1)[0].lower(), locale.rsplit("-", 1)[-1]
    if region.upper() == country:
        return True
    territory = SELECTOR.root.find(f"territory[@type='{country}']")
    if territory is None:
        return False
    ranked = sorted(
        (
            (
                (item.get("type") or "").split("_", 1)[0].lower(),
                float(item.get("populationPercent") or 0),
            )
            for item in territory.findall("languagePopulation")
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return language in {name for name, _ in ranked[:2]}


def _primary_locale(country: str) -> str:
    """The most widely spoken language *in that country*, not CLDR's own pick.

    ``SELECTOR.from_region`` answers es-US for the United States, de-FR for
    France and bar-DE for Germany: it takes CLDR's ordering, which is neither by
    population nor by "what a browser there would report".
    """
    from camoufox.locale import SELECTOR

    territory = SELECTOR.root.find(f"territory[@type='{country}']")
    if territory is not None:
        ranked = sorted(
            (
                (
                    (item.get("type") or "").split("_", 1)[0].lower(),
                    float(item.get("populationPercent") or 0),
                )
                for item in territory.findall("languagePopulation")
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if ranked and ranked[0][0]:
            return f"{ranked[0][0]}-{country}"
    return "en-US"


def _offset(timezone: str) -> int:
    return int(datetime.now(tz=ZoneInfo(timezone)).utcoffset().total_seconds())


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkspaceError(f"出口时区无法识别：{value}") from None
    return value


def read_state(account) -> dict:
    path = account.profile / STATE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve(account, proxy: dict[str, str] | None) -> Egress:
    """Probe the exit and reconcile it with the account's optional pins."""
    settings = account.browser
    # A custom probe is the one the user proved shares the target's routing, so it
    # replaces the defaults instead of being cross-checked against differently
    # routed endpoints.
    urls = (settings.probe_url,) if settings.probe_url else DEFAULT_PROBES
    answers = _probe_many(urls, proxy, custom=bool(settings.probe_url))
    answer = answers[0]

    located = _locate(answer["ip"])
    country = answer["country"] or (located[3] if located else "")
    if not country:
        raise WorkspaceError(f"出口 {answer['ip']} 无法解析出国家；位置未知时拒绝启动")
    stored = read_state(account)
    if settings.egress == "strict" and stored.get("country") not in {None, country}:
        raise WorkspaceError(
            f"出口国家已从 {stored['country']} 变为 {country}；"
            'strict 策略下拒绝带着旧身份启动（确认后用 egress="auto" 才能跟随迁移）'
        )

    zone = located[0] if located else answer["timezone"]
    if not zone:
        raise WorkspaceError(f"出口 {answer['ip']} 无法解析出时区；位置未知时拒绝启动")
    timezone = _timezone(zone)
    latitude = located[1] if located else answer["latitude"]
    longitude = located[2] if located else answer["longitude"]
    if latitude is None or longitude is None:
        raise WorkspaceError(f"出口 {answer['ip']} 无法解析出坐标；位置未知时拒绝启动")

    if settings.locale:
        if not _spoken(country, settings.locale):
            raise WorkspaceError(
                f"配置的 locale={settings.locale} 在出口国家 {country} 不常用；"
                "浏览器语言会与 IP 不符"
            )
        locale = settings.locale
    elif stored.get("country") == country and stored.get("locale"):
        locale = stored["locale"]
    else:
        locale = _primary_locale(country)

    if settings.timezone:
        # Same-country zones are usually within a few hours of each other (New
        # York vs Los Angeles is 3h); a cross-continent pin is much further off.
        if abs(_offset(settings.timezone) - _offset(timezone)) > MAX_PIN_OFFSET:
            raise WorkspaceError(
                f"配置的 timezone={settings.timezone} 与出口时区 {timezone} 相差过大；"
                "浏览器时间会与 IP 不符"
            )
        timezone = settings.timezone

    return Egress(
        ip=answer["ip"],
        country=country,
        timezone=timezone,
        latitude=latitude,
        longitude=longitude,
        locale=locale,
    )


def remember(account, egress: Egress) -> None:
    from .storage import write_json

    write_json(account.profile / STATE, egress.state())


def identity_from_config(account) -> Egress | None:
    """Best-effort identity for egress="off": no probe, only configured values."""
    settings = account.browser
    if not settings.timezone and not settings.locale:
        return None
    timezone = _timezone(settings.timezone or "UTC")
    locale = settings.locale or "en-US"
    return Egress(
        ip="0.0.0.0",
        country=(locale.split("-")[1] if "-" in locale else "US"),
        timezone=timezone,
        latitude=0.0,
        longitude=0.0,
        locale=locale,
    )


def apply(config: dict, identity: Egress | None, webrtc: str) -> dict:
    """Overlay the identity on the frozen random fingerprint config."""
    from camoufox.locale import handle_locale

    result = dict(config)
    for key in (
        "timezone",
        "locale:language",
        "locale:region",
        "locale:script",
        "locale:all",
        "geolocation:longitude",
        "geolocation:latitude",
        "geolocation:accuracy",
        "webrtc:ipv4",
        "webrtc:ipv6",
    ):
        result.pop(key, None)
    if identity is None:
        return result
    locale = handle_locale(identity.locale)
    result.update(locale.as_config())
    result["locale:all"] = locale.as_string
    result["timezone"] = identity.timezone
    if identity.ip != "0.0.0.0":  # identity_from_config has no real location
        result["geolocation:longitude"] = identity.longitude
        result["geolocation:latitude"] = identity.latitude
        result["geolocation:accuracy"] = identity.accuracy
    if webrtc == "spoof":
        if ":" in identity.ip:
            result["webrtc:ipv6"] = identity.ip
        else:
            result["webrtc:ipv4"] = identity.ip
    return result


def prefs(identity: Egress | None, webrtc: str) -> dict:
    """Preferences that close the signals Camoufox's config does not cover."""
    common = {
        # Never let the host's locale formatting or geolocation providers shine through.
        "intl.regional_prefs.use_os_locales": False,
        "javascript.use_us_english_locale": False,
        "geo.provider.use_corelocation": False,
        "geo.provider.use_geoclue": False,
        # With a proxy, DNS must be resolved at the exit, and DoH must not bypass it.
        "network.proxy.socks_remote_dns": True,
        "network.trr.mode": 0,
        "media.peerconnection.ice.obfuscate_host_addresses": True,
    }
    if identity is None:
        return common
    common.update(
        {
            "general.useragent.locale": identity.locale,
            "intl.accept_languages": identity.locale,
        }
    )
    if webrtc == "block":
        common["media.peerconnection.enabled"] = False
    elif webrtc == "spoof":
        common["media.peerconnection.enabled"] = True
        # Firefox would otherwise advertise IPv6 candidates the exit cannot carry.
        if ":" not in identity.ip:
            common["network.dns.disableIPv6"] = True
    return common


# Deliberately no getCurrentPosition(): with a spoofed geolocation config the
# bundled build blocks the page's event loop until the provider answers, so the
# whole evaluate can hang indefinitely (a JS-side setTimeout never fires).
# Timezone and language are injected and readable synchronously; coordinates are
# static config and are covered by the unit tests instead.
OBSERVE = """() => ({
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  language: navigator.language,
  languages: navigator.languages,
})"""


def compare(identity: Egress, observed: dict) -> list[str]:
    problems = []
    if observed.get("timezone") != identity.timezone:
        problems.append(f"时区 {observed.get('timezone')} != {identity.timezone}")
    language = (observed.get("language") or "").lower()
    if language != identity.locale.lower():
        problems.append(f"语言 {language or '?'} != {identity.locale}")
    coords = observed.get("coords")
    if coords and identity.latitude:
        if abs(coords[0] - identity.latitude) > 1 or abs(coords[1] - identity.longitude) > 1:
            problems.append("地理坐标与出口不符")
    return problems


def verify_page(page, identity: Egress) -> dict:
    """Read the signals back from a live document; blank pages get no injection."""
    # A start URL that redirects (a signed-in account's /login -> /new) can tear the
    # execution context down while the read is in flight; that is not a mismatch.
    error = None
    for attempt in range(3):
        try:
            observed = page.evaluate(OBSERVE)
            break
        except Exception as failure:  # noqa: BLE001 - navigation may destroy the context
            error = failure
            if attempt < 2:
                page.wait_for_timeout(1000)
    else:
        return {"egress_verified": False, "egress_error": f"无法读取浏览器特征：{error}"}
    problems = compare(identity, observed)
    if problems:
        return {"egress_verified": False, "egress_error": "；".join(problems)}
    return {"egress_verified": True}
