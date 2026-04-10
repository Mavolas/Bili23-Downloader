from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
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


DEFAULT_URL = "https://www.bilibili.com/video/BV1nhCGYpECj/"


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


def _download_stream(url: str, out_path: Path, referer: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": referer, "User-Agent": config.get(config.user_agent)}

    cookies = get_cookies()
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

    playurl = _get_playurl(bvid, cid, qn=80)
    v_url, a_url = _pick_best_dash(playurl)

    temp_dir = out_dir / "_tmp" / bvid
    video_path = temp_dir / "video.m4s"
    audio_path = temp_dir / "audio.m4s"
    merged_path = out_dir / f"{title}.mp4"

    print(f"标题: {title}")
    print(f"BV: {bvid}  CID: {cid}")

    _download_stream(v_url, video_path, referer=referer)
    _download_stream(a_url, audio_path, referer=referer)

    if _merge_ffmpeg(video_path, audio_path, merged_path):
        print(f"已合并输出: {merged_path}")
        return merged_path

    # 没有 ffmpeg 就保留分离文件
    fallback_video = out_dir / f"{title}.video.m4s"
    fallback_audio = out_dir / f"{title}.audio.m4s"
    fallback_video.parent.mkdir(parents=True, exist_ok=True)
    fallback_video.write_bytes(video_path.read_bytes())
    fallback_audio.write_bytes(audio_path.read_bytes())
    print("未检测到 ffmpeg，已输出分离文件:")
    print(f"- {fallback_video}")
    print(f"- {fallback_audio}")
    return fallback_video


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 bilibili 单个视频（BV 链接）")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="视频链接或 BV 号（默认用脚本内置示例）")
    ap.add_argument("-o", "--out", default=str(Path.cwd() / "downloads"), help="输出目录")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    download_single(args.url, out_dir)


if __name__ == "__main__":
    main()
