"""A股数据源网络诊断脚本。

排查「获取不到 A 股」的三层根因：
  ① Python SSL 根证书是否配置正确（macOS 上 python.org 安装包常缺证书）
  ② 全局代理是否拦截了国内数据源（Clash/V2Ray 等）
  ③ AkShare 能否真正拉到 A 股 K 线

用法：
    ./venv/bin/python tools/diag_ashare_network.py

读完输出后按提示操作即可定位问题。
"""
from __future__ import annotations

import os
import ssl
import sys
import time
import urllib.request

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

# 东方财富 K 线接口（akshare 底层用的就是它）
EM_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55"
    "&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=1"
    "&secid=1.600519&beg=20260601&end=20260626"
)


def _say(mark: str, msg: str) -> None:
    print(f"  {mark} {msg}")


def check_python_ca() -> bool:
    """① 检查 Python 的 SSL 默认 CA 证书是否配置。"""
    print("\n[1/3] 检查 Python SSL 根证书 (cafile)")
    paths = ssl.get_default_verify_paths()
    _say(PASS if paths.cafile else WARN, f"cafile = {paths.cafile or '(未配置!)'}")
    _say(PASS if paths.capath else WARN, f"capath = {paths.capath or '(未配置!)'}")

    # 尝试加载 certifi 作为对照
    try:
        import certifi

        _say(PASS, f"certifi 已安装: {certifi.where()} (v{certifi.__version__})")
        certifi_ok = True
    except ImportError:
        _say(FAIL, "certifi 未安装 (akshare/tvdatafeed 依赖它验证 HTTPS)")
        certifi_ok = False

    if not paths.cafile:
        print(
            "\n  >>> 问题①：Python 的默认 CA 证书未配置。这是 macOS 上 python.org 安装包"
            "\n     未运行 Install Certificates.command 导致的。\n"
            "     修复：双击运行 /Applications/Python 3.11/Install Certificates.command\n"
            "     或：  pip install --upgrade certifi 并设置 SSL_CERT_FILE 环境变量。"
        )
        return False
    return True or certifi_ok


def check_proxy() -> tuple[bool, dict[str, str]]:
    """② 检查系统/进程代理是否会路由到国内数据源。"""
    print("\n[2/3] 检查代理配置")
    proxies = urllib.request.getproxies()
    if proxies:
        _say(WARN, f"检测到系统代理: {proxies}")
        _say(
            WARN,
            "国内数据源(eastmoney/sina)走海外代理常被拒绝 → ProxyError/RemoteDisconnected。\n"
            "      修复：在代理软件(Clash等)规则里把这些域名设为 DIRECT，或临时关闭系统代理。",
        )
        return False, proxies
    _say(PASS, "未检测到系统代理 (国内源可直连)")
    return True, {}


def _fetch_with(opener: urllib.request.OpenerDirector, label: str) -> tuple[bool, str]:
    req = urllib.request.Request(EM_URL, headers={"User-Agent": "Mozilla/5.0"})
    t = time.time()
    try:
        resp = opener.open(req, timeout=15)
        data = resp.read()
        ok = resp.status == 200 and len(data) > 50
        _say(
            PASS if ok else FAIL,
            f"{label}: HTTP {resp.status}, {len(data)} bytes ({time.time()-t:.1f}s)",
        )
        return ok, ""
    except Exception as exc:  # noqa: BLE001
        _say(FAIL, f"{label}: {type(exc).__name__}: {str(exc)[:120]} ({time.time()-t:.1f}s)")
        return False, str(exc)


def check_real_fetch() -> bool:
    """③ 实测能否拉到东方财富数据（分别用 certifi 证书 + 强制无代理）。"""
    print("\n[3/3] 实测东方财富 K 线接口")
    # 强制无代理，避免被 Clash TUN/系统代理拦截
    no_proxy = urllib.request.ProxyHandler({})
    any_ok = False

    # 方式A：用 certifi 的 CA 包
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        opener_a = urllib.request.build_opener(
            no_proxy, urllib.request.HTTPSHandler(context=ctx)
        )
        ok_a, _ = _fetch_with(opener_a, "certifi证书 + 无代理")
        any_ok = any_ok or ok_a
    except ImportError:
        _say(FAIL, "方式A跳过：certifi 未安装")

    # 方式B：用系统默认 CA（验证 Install Certificates 是否生效）
    opener_b = urllib.request.build_opener(no_proxy)
    ok_b, err_b = _fetch_with(opener_b, "系统默认CA + 无代理")
    any_ok = any_ok or ok_b

    return any_ok


def check_akshare() -> bool:
    """④ AkShare 端到端验证：分别测东财源与新浪源，再用项目自己的 AkShareSource。"""
    print("\n[4/4] AkShare 端到端验证 (贵州茅台 600519 日线)")
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        _say(FAIL, "akshare 未安装: pip install akshare")
        return False

    em_ok = sina_ok = False
    # 东财源（push2his，部分网络不稳定）
    t = time.time()
    try:
        df = ak.stock_zh_a_hist(
            symbol="600519", period="daily",
            start_date="20260601", end_date="20260626", adjust="qfq",
        )
        if df is None or df.empty:
            _say(WARN, f"东财源(stock_zh_a_hist): 返回空 ({time.time()-t:.1f}s)")
        else:
            em_ok = True
            _say(PASS, f"东财源(stock_zh_a_hist): {len(df)}根 ({time.time()-t:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        _say(WARN, f"东财源(stock_zh_a_hist): {type(exc).__name__} ({time.time()-t:.1f}s)")
        _say("   ", "（东财 push2his 接口在部分网络下不稳定，项目已改为新浪优先）")

    # 新浪源（项目主用源，更稳定）
    t = time.time()
    try:
        df = ak.stock_zh_a_daily(
            symbol="sh600519", start_date="20260601", end_date="20260626", adjust="qfq"
        )
        if df is None or df.empty:
            _say(FAIL, f"新浪源(stock_zh_a_daily): 返回空 ({time.time()-t:.1f}s)")
        else:
            sina_ok = True
            _say(PASS, f"新浪源(stock_zh_a_daily): {len(df)}根, 收盘={df.iloc[-1]['close']} ({time.time()-t:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        _say(FAIL, f"新浪源(stock_zh_a_daily): {type(exc).__name__}: {str(exc)[:120]} ({time.time()-t:.1f}s)")

    # 项目自己的数据源（最贴近真实使用）
    t = time.time()
    try:
        from pa_agent.data.akshare_source import AkShareSource

        src = AkShareSource()
        src.connect()
        src.subscribe("600519", "1d")
        bars = src.latest_snapshot(3)
        src.disconnect()
        if bars:
            _say(
                PASS,
                f"项目AkShareSource: {len(bars)}根K线, 最新收盘={bars[-1].close} ({time.time()-t:.1f}s)",
            )
            return True
        _say(FAIL, f"项目AkShareSource: 返回空 ({time.time()-t:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        _say(FAIL, f"项目AkShareSource: {type(exc).__name__}: {str(exc)[:140]} ({time.time()-t:.1f}s)")

    return sina_ok


def main() -> int:
    print("=" * 64)
    print(" A 股数据源网络诊断  (tools/diag_ashare_network.py)")
    print("=" * 64)
    ca_ok = check_python_ca()
    check_proxy()
    fetch_ok = check_real_fetch()
    ak_ok = check_akshare()

    print("\n" + "=" * 64)
    if ak_ok:
        print(f" {PASS} 结论：A 股数据源可用。可在 GUI 选 AkShare(A股) 取数。")
        return 0
    print(f" {FAIL} 结论：当前无法获取 A 股数据。按上面的「修复」提示处理后重跑本脚本。")
    if not ca_ok:
        print("   主要原因①：Python SSL 根证书未配置 (先修这个)")
    if not fetch_ok:
        print("   主要原因②/③：代理拦截或证书校验失败，导致 HTTPS 被拒/断开")
    print("   若东财源失败但新浪源成功：属正常（东财push2his接口不稳），项目已用新浪源兜底。")
    print("   若新浪源也失败：检查网络/代理对 hq.sinajs.cn 的访问。")
    print("=" * 64)
    return 1


if __name__ == "__main__":
    sys.exit(main())
