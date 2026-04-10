from __future__ import annotations

import argparse
import time
import sys
import os
import json
from pathlib import Path
from urllib.parse import urlencode

import qrcode
import httpx

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from util.common.config import config, config_path  # type: ignore  # noqa: E402
from util.common.enum import QRCodeScanStatus  # type: ignore  # noqa: E402
from util.network.request import SyncNetWorkRequest, client, update_cookies  # type: ignore  # noqa: E402


COOKIES_DIR = ROOT / "cookies"

def _pick_cookie_from_jar(name: str) -> str:
    """解决 httpx CookieConflict：同名 cookie 多条时，优先 .bilibili.com + path=/。"""
    target = (name or "").lower()
    if not target:
        return ""

    jar = getattr(client.cookies, "jar", None)
    if jar is None:
        return ""

    candidates: list[tuple[int, str]] = []
    for c in jar:
        try:
            if (c.name or "").lower() != target:
                continue
            domain = (c.domain or "").lstrip(".").lower()
            path = c.path or ""
            score = 0
            if domain.endswith("bilibili.com"):
                score += 10
            if domain == "bilibili.com":
                score += 2
            if path == "/":
                score += 3
            candidates.append((score, str(c.value or "")))
        except Exception:
            continue

    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _safe_cookie_get(name: str, default: str = "") -> str:
    try:
        v = client.cookies.get(name, default)
        return str(v) if v is not None else str(default)
    except Exception:
        v = _pick_cookie_from_jar(name)
        return v if v else str(default)


def _cookies_from_httpx_client_deduped() -> dict:
    """从 jar 导出 cookies 并去重（大小写不敏感），但保留“正确的”原始名称大小写。"""
    jar = getattr(client.cookies, "jar", None)
    if jar is None:
        return {k: v for k, v in client.cookies.items()}

    # lower_name -> (score, original_name, value)
    best: dict[str, tuple[int, str, str]] = {}
    for c in jar:
        try:
            name = (c.name or "").strip()
            if not name:
                continue
            key = name.lower()
            domain = (c.domain or "").lstrip(".").lower()
            path = c.path or ""
            score = 0
            if domain.endswith("bilibili.com"):
                score += 10
            if domain == "bilibili.com":
                score += 2
            if path == "/":
                score += 3
            val = str(c.value or "")
            if key not in best or score > best[key][0]:
                best[key] = (score, name, val)
        except Exception:
            continue

    # 输出时使用挑选出来的 original_name，避免把 SESSDATA 等写成 sessdata
    return {name: val for _, (_, name, val) in best.items() if val}


def _mask(v: str, keep_start: int = 3, keep_end: int = 3) -> str:
    v = v or ""
    if len(v) <= keep_start + keep_end:
        return "*" * len(v) if v else ""
    return f"{v[:keep_start]}***{v[-keep_end:]}"


def _print_written_cookies() -> None:
    pairs = [
        ("SESSDATA", _safe_cookie_get("SESSDATA", "")),
        ("bili_jct", _safe_cookie_get("bili_jct", "")),
        ("DedeUserID", _safe_cookie_get("DedeUserID", "")),
        ("DedeUserID__ckMd5", _safe_cookie_get("DedeUserID__ckMd5", "")),
        ("buvid3", _safe_cookie_get("buvid3", "")),
        ("buvid4", _safe_cookie_get("buvid4", "")),
        ("b_lsid", _safe_cookie_get("b_lsid", "")),
        ("b_nut", _safe_cookie_get("b_nut", "")),
        ("_uuid", _safe_cookie_get("_uuid", "")),
    ]

    written = [(k, str(v)) for k, v in pairs if v]
    if not written:
        print("本次未检测到可写入的 Cookie（可能登录未完成或网络请求未落到同一会话）。")
        return

    print("已写入/更新的 Cookie（脱敏显示）:")
    for k, v in written:
        print(f"- {k}={_mask(v)}")


def _sanitize_part(s: str) -> str:
    s = (s or "").strip().replace(" ", "_")
    s = "".join(ch for ch in s if ch not in '\\/:*?"<>|')
    s = s.strip("._-")
    return s or "unknown"


def _cookie_file_name(uid: str | int | None, uname: str | None, created_ts: int) -> str:
    uid_s = str(uid) if uid else "unknown"
    uname_s = _sanitize_part(uname or "unknown")
    return f"{uid_s}_{uname_s}_{created_ts}.json"


def _cookies_from_httpx_client() -> dict:
    return _cookies_from_httpx_client_deduped()


def _is_cookie_valid(cookies: dict, timeout: float = 8.0) -> tuple[bool, dict]:
    """用 /x/web-interface/nav 判断是否登录有效。返回 (valid, nav_data)。"""
    try:
        with httpx.Client(
            headers={"Referer": "https://www.bilibili.com/", "User-Agent": config.get(config.user_agent)},
            cookies=cookies,
            follow_redirects=True,
            timeout=timeout,
        ) as c:
            r = c.get("https://api.bilibili.com/x/web-interface/nav")
            r.raise_for_status()
            j = r.json()
        data = j.get("data", {}) if isinstance(j, dict) else {}
        return bool(data.get("isLogin")), data
    except Exception:
        return False, {}


def cleanup_expired_cookie_files() -> None:
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    removed = 0
    for p in sorted(COOKIES_DIR.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            cookies = raw.get("cookies", {}) if isinstance(raw, dict) else {}
            if not isinstance(cookies, dict) or not cookies:
                p.unlink(missing_ok=True)
                removed += 1
                continue
            ok, _ = _is_cookie_valid(cookies)
            if not ok:
                p.unlink(missing_ok=True)
                removed += 1
        except Exception:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
    if removed:
        print(f"已自动清理过期/无效 Cookie 文件: {removed} 个（目录: {COOKIES_DIR}）")


def save_cookie_file_from_current_session() -> Path:
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    cookies = _cookies_from_httpx_client()
    # 校验改为：直接使用当前会话（同一个全局 client）访问 nav，
    # 避免因导出的 cookies dict 大小写/去重导致校验误判。
    nav_resp = SyncNetWorkRequest("https://api.bilibili.com/x/web-interface/nav").run()
    nav_data = (nav_resp.get("data", {}) if isinstance(nav_resp, dict) else {}) if isinstance(nav_resp, dict) else {}
    if not isinstance(nav_data, dict) or not nav_data.get("isLogin"):
        raise RuntimeError("当前会话 Cookie 未通过有效性校验，无法保存")

    uid = nav_data.get("mid")
    uname = nav_data.get("uname")
    created_ts = int(time.time())

    out_path = COOKIES_DIR / _cookie_file_name(uid, uname, created_ts)
    payload = {
        "schema": 1,
        "created_at": created_ts,
        "uid": uid,
        "uname": uname,
        "cookies": cookies,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _print_qr_ascii(data: str) -> None:
    # 尽量在 Windows 终端下不乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
    except Exception:
        pass

    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    mat = qr.get_matrix()

    # 某些控制台/字体对 Unicode 方块支持不好，优先使用 ASCII
    black = "##"
    white = "  "
    for row in mat:
        print("".join(black if cell else white for cell in row))


def _save_qr_png(data: str, out_path: Path) -> None:
    img = qrcode.make(data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def _open_file(path: Path) -> None:
    # Windows: os.startfile；其他平台尽量不做额外依赖
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def _sync_set_login_cookies_from_client() -> None:
    # 这些 cookie 是 download.py 最关键的登录态字段
    config.set(config.bili_jct, _safe_cookie_get("bili_jct", ""))
    config.set(config.DedeUserID, _safe_cookie_get("DedeUserID", ""))
    config.set(config.DedeUserID__ckMd5, _safe_cookie_get("DedeUserID__ckMd5", ""))
    config.set(config.SESSDATA, _safe_cookie_get("SESSDATA", ""))
    config.set(config.is_login, True)
    config.is_expired = False

    # 顺手补充一些常用的设备指纹 cookie（有则写入）
    for k, cfg_key in [
        ("buvid3", config.buvid3),
        ("buvid4", config.buvid4),
        ("b_nut", config.b_nut),
        ("b_lsid", config.b_lsid),
        ("_uuid", config.uuid),
    ]:
        v = _safe_cookie_get(k, "")
        if v:
            config.set(cfg_key, str(v))

    update_cookies()


def _status_text(code: int) -> str:
    if code == QRCodeScanStatus.WAITING_FOR_SCAN:
        return "等待扫码"
    if code == QRCodeScanStatus.WAITING_FOR_CONFIRMATION:
        return "已扫码，等待手机确认"
    if code == QRCodeScanStatus.EXPIRED:
        return "二维码已过期"
    if code == QRCodeScanStatus.SUCCESS:
        return "登录成功"
    return f"未知状态({code})"


def qr_login(timeout_s: int = 180, save_png: Path | None = None, open_png: bool = False) -> None:
    cleanup_expired_cookie_files()

    # 生成二维码
    params = {
        "source": "main-fe-header",
        "go_url": "https://www.bilibili.com/",
        "web_location": "333.1007",
    }
    gen_url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/generate?{urlencode(params)}"
    gen_resp = SyncNetWorkRequest(gen_url).run()

    if not isinstance(gen_resp, dict) or gen_resp.get("code", -1) != 0:
        raise RuntimeError(gen_resp.get("message", "生成二维码失败") if isinstance(gen_resp, dict) else "生成二维码失败")

    qr_url = gen_resp["data"]["url"]
    qr_key = gen_resp["data"]["qrcode_key"]

    print("请使用哔哩哔哩 App 扫码登录。")
    print()
    _print_qr_ascii(qr_url)
    print()
    print(f"二维码链接: {qr_url}")

    if save_png:
        _save_qr_png(qr_url, save_png)
        print(f"已保存二维码图片: {save_png}")
        if open_png:
            _open_file(save_png)

    # 轮询扫码状态
    poll_url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qr_key}"
    start = time.time()
    last_code: int | None = None

    while True:
        if time.time() - start > timeout_s:
            raise TimeoutError("登录超时，请重试")

        resp = SyncNetWorkRequest(poll_url).run()
        if not isinstance(resp, dict) or resp.get("code", -1) != 0:
            raise RuntimeError(resp.get("message", "轮询失败") if isinstance(resp, dict) else "轮询失败")

        code = int(resp["data"]["code"])
        if code != last_code:
            print(_status_text(code))
            last_code = code

        if code == QRCodeScanStatus.SUCCESS:
            # 有些情况下需要访问返回的跳转 URL 才会完整下发 Cookie
            succ_url = resp.get("data", {}).get("url") if isinstance(resp.get("data", {}), dict) else None
            if succ_url:
                try:
                    client.get(str(succ_url))
                except Exception:
                    pass

            _sync_set_login_cookies_from_client()
            cookie_file = save_cookie_file_from_current_session()
            print("已写入登录 Cookie 到配置 + cookies 目录文件，download.py 可直接复用。")
            print(f"配置文件路径: {config_path}")
            print(f"Cookie 文件路径: {cookie_file}")
            print(f"Cookie 目录: {COOKIES_DIR}")
            _print_written_cookies()
            return

        if code == QRCodeScanStatus.EXPIRED:
            raise RuntimeError("二维码已过期，请重新运行脚本")

        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Bili23 Downloader 扫码登录（写入 Cookie 供脚本复用）")
    ap.add_argument("--timeout", type=int, default=180, help="超时时间（秒）")
    ap.add_argument("--png", default=str(Path.cwd() / "bili_qrcode.png"), help="保存二维码 PNG 的路径")
    ap.add_argument("--no-png", action="store_true", help="不保存 PNG（只在终端显示）")
    ap.add_argument("--open", action="store_true", help="保存 PNG 后自动打开图片")
    args = ap.parse_args()

    save_png = None if args.no_png else Path(args.png).expanduser().resolve()
    qr_login(timeout_s=args.timeout, save_png=save_png, open_png=args.open)


if __name__ == "__main__":
    main()
