from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import json
import importlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx

# 复用项目内的 Cookie 与 WBI 签名逻辑（需要 PySide6 / qfluentwidgets 的 config 初始化）
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from util.common.config import config  # type: ignore  # noqa: E402
from util.network.request import SyncNetWorkRequest, get_cookies  # type: ignore  # noqa: E402
from util.parse.parser.base import ParserBase  # type: ignore  # noqa: E402


DEFAULT_URL = ""
DEFAULT_LIST_FILE = ROOT / "download-list.txt"
COOKIES_DIR = ROOT / "cookies"
_COOKIE_OVERRIDE: Path | None = None


def _read_download_list(list_path: Path) -> list[str]:
    """一行一个链接；空行跳过；# 开头视为注释。"""
    if not list_path.is_file():
        raise FileNotFoundError(f"列表文件不存在: {list_path}")
    urls: list[str] = []
    for raw in list_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _get_default_output_dir() -> Path:
    """默认输出目录来自项目根目录的 `config.py`。"""
    try:
        # 这里 import 的是项目根目录下的 config.py（不是 util.common.config）
        local_cfg = importlib.import_module("config")
        p = getattr(local_cfg, "DOWNLOAD_OUTPUT_DIR", None)
        if p:
            return Path(p).expanduser()
    except Exception:
        pass
    return Path.cwd() / "downloads"


def _load_cookie_file(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    cookies = raw.get("cookies", {})
    if not isinstance(cookies, dict):
        return {}
    # 规范化：大小写不敏感去重，但输出“规范大小写”的 cookie 名（不能一律转小写）
    canonical = {
        "sessdata": "SESSDATA",
        "bili_jct": "bili_jct",
        "dedeuserid": "DedeUserID",
        "dedeuserid__ckmd5": "DedeUserID__ckMd5",
        "buvid3": "buvid3",
        "buvid4": "buvid4",
        "b_lsid": "b_lsid",
        "b_nut": "b_nut",
        "_uuid": "_uuid",
    }

    best: dict[str, tuple[int, str, str]] = {}
    for k, v in cookies.items():
        if v is None:
            continue
        name = str(k)
        val = str(v)
        low = name.lower()
        # 偏好 canonical 名称
        score = 1
        if canonical.get(low) == name:
            score = 3
        elif name == low:
            score = 2
        if low not in best or score > best[low][0]:
            best[low] = (score, canonical.get(low, name), val)

    return {name: val for _, (_, name, val) in best.items() if val}


def _cookie_is_valid(cookies: dict) -> bool:
    try:
        with httpx.Client(
            headers={"Referer": "https://www.bilibili.com/", "User-Agent": config.get(config.user_agent)},
            cookies=cookies,
            follow_redirects=True,
            timeout=8,
        ) as c:
            r = c.get("https://api.bilibili.com/x/web-interface/nav")
            r.raise_for_status()
            j = r.json()
        data = j.get("data", {}) if isinstance(j, dict) else {}
        return bool(data.get("isLogin"))
    except Exception:
        return False


def _pick_latest_valid_cookie_file() -> Path | None:
    if not COOKIES_DIR.exists():
        return None
    candidates = sorted(COOKIES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            cookies = _load_cookie_file(p)
            if cookies and _cookie_is_valid(cookies):
                return p
            # 无效则自动清理
            p.unlink(missing_ok=True)
        except Exception:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return None


def _get_effective_cookies() -> dict:
    if _COOKIE_OVERRIDE:
        cookies = _load_cookie_file(_COOKIE_OVERRIDE)
        return cookies or get_cookies()

    p = _pick_latest_valid_cookie_file()
    if p:
        cookies = _load_cookie_file(p)
        if cookies:
            return cookies

    return get_cookies()


class _Wbi(ParserBase):
    """给脚本复用 ParserBase.enc_wbi"""


def _init_wbi_keys() -> None:
    """同步获取 wbi img_key/sub_key，否则所有 wbi 接口会签名失败。"""
    nav = SyncNetWorkRequest("https://api.bilibili.com/x/web-interface/nav").run()
    data = nav.get("data", {}) if isinstance(nav, dict) else {}
    wbi_img = data.get("wbi_img", {}) if isinstance(data, dict) else {}
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")

    def _stem(u: str) -> str:
        # url 末尾通常为 xxxx.png，Path(stem) 可取 xxxx
        try:
            return Path(urlparse(u).path).stem
        except Exception:
            return ""

    img_key = _stem(img_url)
    sub_key = _stem(sub_url)
    if img_key and sub_key:
        config.set(config.img_key, img_key, save=False)
        config.set(config.sub_key, sub_key, save=False)


def _extract_bvid(url_or_bvid: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", url_or_bvid)
    if not m:
        raise ValueError("未在输入中找到 BV 号")
    return m.group(1)


def _extract_p(url: str) -> int | None:
    try:
        q = parse_qs(urlparse(url).query)
        if "p" not in q:
            return None
        return int(q["p"][0])
    except Exception:
        return None


def _sanitize_filename(name: str) -> str:
    name = name.strip().strip(".")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "bilibili_video"


def _wbi_get_json(url: str) -> dict:
    resp = SyncNetWorkRequest(url).run()
    if not isinstance(resp, dict):
        raise RuntimeError("接口响应不是 JSON")
    if resp.get("code", -1) != 0:
        raise RuntimeError(resp.get("message", "接口返回错误"))
    return resp


def _get_view(bvid: str) -> dict:
    wbi = _Wbi()
    params = {"bvid": bvid}
    url = f"https://api.bilibili.com/x/web-interface/wbi/view?{wbi.enc_wbi(params)}"
    return _wbi_get_json(url)


def _get_playurl(bvid: str, cid: int, qn: int = 80) -> dict:
    wbi = _Wbi()
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnver": 0,
        "fnval": 4048,
        "fourk": 1,
    }
    url = f"https://api.bilibili.com/x/player/wbi/playurl?{wbi.enc_wbi(params)}"
    return _wbi_get_json(url)


def _ensure_1080p_available(playurl_resp: dict) -> None:
    """没有 1080P( qn=80 )就不下载。"""
    data = playurl_resp.get("data", {}) if isinstance(playurl_resp, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError("playurl 返回结构异常")

    accept = data.get("accept_quality", [])
    if isinstance(accept, list) and accept:
        try:
            accept_int = {int(x) for x in accept}
        except Exception:
            accept_int = set()
        if 80 not in accept_int:
            raise RuntimeError("该视频不提供 1080P（80），已按设置跳过下载")
        return

    # 兜底：有些情况下 accept_quality 为空，改查 dash.video 的 id
    dash = data.get("dash", {})
    videos = dash.get("video", []) if isinstance(dash, dict) else []
    try:
        qids = {int(v.get("id", -1)) for v in videos if isinstance(v, dict)}
    except Exception:
        qids = set()
    if 80 not in qids:
        raise RuntimeError("该视频不提供 1080P（80），已按设置跳过下载")


def _pick_best_dash(playurl_data: dict) -> tuple[str, str]:
    data = playurl_data.get("data", {}) if isinstance(playurl_data, dict) else {}
    dash = data.get("dash", {}) if isinstance(data, dict) else {}

    videos = dash.get("video", []) if isinstance(dash, dict) else []
    audios = dash.get("audio", []) if isinstance(dash, dict) else []

    # 优先 flac / dolby（如果存在）
    flac_audio = None
    dolby_audio = None
    if isinstance(dash, dict):
        flac = dash.get("flac", {})
        if isinstance(flac, dict) and isinstance(flac.get("audio"), dict):
            flac_audio = flac["audio"]
        dolby = dash.get("dolby", {})
        if isinstance(dolby, dict) and isinstance(dolby.get("audio"), list) and dolby["audio"]:
            dolby_audio = dolby["audio"][0]

    def _best(items: list[dict]) -> dict | None:
        if not items:
            return None
        return max(items, key=lambda x: (x.get("bandwidth", 0), x.get("id", 0)))

    v = _best(videos)
    a = flac_audio or dolby_audio or _best(audios)

    if not v or not a:
        raise RuntimeError("未拿到 DASH 音视频地址（可能需要登录或该视频不支持）")

    v_url = v.get("baseUrl") or v.get("base_url") or ""
    a_url = a.get("baseUrl") or a.get("base_url") or ""
    if not v_url or not a_url:
        raise RuntimeError("DASH 返回缺少 baseUrl")

    return v_url, a_url


def _pick_1080p_video_only(playurl_data: dict) -> str:
    """只取 1080P 视频流（qn=80），不取音频。"""
    data = playurl_data.get("data", {}) if isinstance(playurl_data, dict) else {}
    dash = data.get("dash", {}) if isinstance(data, dict) else {}

    videos = dash.get("video", []) if isinstance(dash, dict) else []
    videos_1080 = [v for v in videos if isinstance(v, dict) and int(v.get("id", -1)) == 80]
    if not videos_1080:
        raise RuntimeError("未找到 1080P（80）的 DASH 视频流")

    # 1080P 内部可能有不同 codec/码率，取带宽最高的
    best = max(videos_1080, key=lambda x: (x.get("bandwidth", 0), x.get("codecid", 0)))
    v_url = best.get("baseUrl") or best.get("base_url") or ""
    if not v_url:
        raise RuntimeError("1080P 视频流缺少 baseUrl")
    return v_url


def _download_stream(url: str, out_path: Path, referer: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": referer, "User-Agent": config.get(config.user_agent)}

    cookies = _get_effective_cookies()
    with httpx.Client(headers=headers, cookies=cookies, follow_redirects=True, timeout=30) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total > 0:
                        pct = done * 100 // total
                        print(f"\r下载 {out_path.name}: {pct}% ({done}/{total})", end="")
            print()


def _merge_ffmpeg(video_path: Path, audio_path: Path, out_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c",
        "copy",
        str(out_path),
    ]
    # Windows 上某些环境默认编码为 GBK，ffmpeg 输出可能含非 GBK 字符导致解码异常
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 合并失败：\n{p.stderr.strip()}")
    return True


def _segment_video_ffmpeg(input_path: Path, out_dir: Path, base_name: str, segment_seconds: int = 600) -> list[Path]:
    """使用 ffmpeg 按固定时长无损切片（仅视频）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未检测到 ffmpeg，无法按 10 分钟切片")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 输出为 mp4 片段（更适合作为剪辑素材），按时间切片并重置时间戳
    out_pattern = out_dir / f"{base_name}_%03d.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(int(segment_seconds)),
        "-reset_timestamps",
        "1",
        str(out_pattern),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 切片失败：\n{p.stderr.strip()}")

    return sorted(out_dir.glob(f"{base_name}_*.mp4"))


def download_single(url_or_bvid: str, out_dir: Path) -> Path:
    _init_wbi_keys()

    bvid = _extract_bvid(url_or_bvid)
    view = _get_view(bvid)
    vdata = view["data"]

    title = _sanitize_filename(vdata.get("title", bvid))
    referer = f"https://www.bilibili.com/video/{bvid}"

    p = _extract_p(url_or_bvid) if "://" in url_or_bvid else None
    if p and isinstance(vdata.get("pages"), list) and 1 <= p <= len(vdata["pages"]):
        cid = int(vdata["pages"][p - 1]["cid"])
    else:
        cid = int(vdata.get("cid"))

    # 强制 1080P：没有 1080P 就不下载
    playurl = _get_playurl(bvid, cid, qn=80)
    _ensure_1080p_available(playurl)
    v_url = _pick_1080p_video_only(playurl)

    temp_dir = out_dir / "_tmp" / bvid
    video_path = temp_dir / "video.m4s"
    segments_dir = out_dir / f"{title}_segments"

    print(f"标题: {title}")
    print(f"BV: {bvid}  CID: {cid}")

    _download_stream(v_url, video_path, referer=referer)

    # 切片（10 分钟一段）
    try:
        segs = _segment_video_ffmpeg(video_path, segments_dir, base_name=title, segment_seconds=600)
        if segs:
            print(f"已切片输出（{len(segs)} 段）: {segments_dir}")
            return segs[0]
        else:
            raise RuntimeError("切片未产生输出文件")
    except Exception as e:
        # 没有 ffmpeg 或切片失败：至少保留 video.m4s
        fallback_video = out_dir / f"{title}.video.m4s"
        fallback_video.parent.mkdir(parents=True, exist_ok=True)
        fallback_video.write_bytes(video_path.read_bytes())
        print(f"切片失败或未安装 ffmpeg：{e}")
        print("已输出原始视频流文件（无音频）:")
        print(f"- {fallback_video}")
        return fallback_video


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 bilibili 视频（单链接或列表文件）")
    ap.add_argument(
        "url",
        nargs="?",
        default=None,
        help="单个视频链接或 BV；不传则从列表文件读取（默认 download-list.txt）",
    )
    ap.add_argument("-o", "--out", default=str(_get_default_output_dir()), help="输出目录（默认读 config.py 的 DOWNLOAD_OUTPUT_DIR）")
    ap.add_argument(
        "--list",
        default=str(DEFAULT_LIST_FILE),
        help=f"下载列表路径，一行一个链接，空行跳过（默认: {DEFAULT_LIST_FILE.name}）",
    )
    ap.add_argument("--cookie", default="", help="指定 cookies 文件（json）。不填则自动使用 cookies/ 下最新可用文件")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    if args.cookie:
        global _COOKIE_OVERRIDE
        _COOKIE_OVERRIDE = Path(args.cookie).expanduser().resolve()

    if args.url:
        download_single(args.url, out_dir)
        return

    list_path = Path(args.list).expanduser().resolve()
    urls = _read_download_list(list_path)
    if not urls:
        print(f"列表为空或无可下载行: {list_path}")
        return

    print(f"从列表读取 {len(urls)} 条: {list_path}")
    for i, u in enumerate(urls, 1):
        print(f"\n===== [{i}/{len(urls)}] {u} =====")
        try:
            download_single(u, out_dir)
        except Exception as e:
            print(f"跳过（失败）: {e}")
            continue


if __name__ == "__main__":
    main()
