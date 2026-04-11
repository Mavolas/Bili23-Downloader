from __future__ import annotations

from pathlib import Path

# `download.py` 默认输出目录（命令行 -o/--out 会覆盖它）
DOWNLOAD_OUTPUT_DIR = Path(r"E:\剪辑工作流\风景素材")

# 下载成片后处理（单位：分钟；需本机已安装 ffmpeg/ffprobe）
# 片头剪掉：从正片开始处算起，删除最前面这么多分钟
VIDEO_TRIM_START_MINUTES = 1.0
# 片尾剪掉：删除最后面这么多分钟
VIDEO_TRIM_END_MINUTES = 1.0
# 中间剩余部分：按每段多少分钟切成素材（与原先「每段 10 分钟」对应，可改小数）
VIDEO_SEGMENT_MINUTES = 10.0
