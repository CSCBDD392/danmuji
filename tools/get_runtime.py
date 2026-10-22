"""下载内置运行时（node.exe）—— 第一次运行 / 缺文件时自动调用。

node.exe 有 90MB，Git 仓库放不下，放在 GitHub Release 里。
正常使用不需要手动跑这个脚本，`python webui.py` 或 `python main.py`
发现缺 node.exe 会自己调用 `ensure_node()`。
"""
import io
import os
import sys
import zipfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENDOR = os.path.join(ROOT, "vendor", "kugou-api")
NODE = os.path.join(VENDOR, "node.exe")

# Release 里的文件（publish_release.py 上传的名字）
ASSET = "node.exe"
OWNER = "CSCBDD392"
REPO = "danmuji"
TAG = "runtime-v1"
URL = "https://github.com/%s/%s/releases/download/%s/%s" % (OWNER, REPO, TAG, ASSET)


def have_node():
    return os.path.isfile(NODE) and os.path.getsize(NODE) > 10 * 1024 * 1024


def _progress(done, total):
    if total <= 0:
        return
    pct = done * 100 // total
    bar = "#" * (pct // 4) + "." * (25 - pct // 4)
    sys.stdout.write("\r  [%s] %3d%%  %.1f/%.1f MB" %
                     (bar, pct, done / 1048576.0, total / 1048576.0))
    sys.stdout.flush()


def download(url=URL, dest=NODE, timeout=60):
    """下载到 dest。支持断点续传不必要 —— 文件是一次性的。"""
    if not os.path.isdir(os.path.dirname(dest)):
        os.makedirs(os.path.dirname(dest))
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "danmuji-runtime"})
    print("正在下载内置运行时（约 90MB，只需一次）：")
    print("  %s" % url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                _progress(done, total)
    print()
    # 大小校验，防止下到半截的 HTML 错误页
    size = os.path.getsize(tmp)
    if size < 10 * 1024 * 1024:
        os.remove(tmp)
        raise RuntimeError("下载下来的文件只有 %d 字节，不像 node.exe" % size)
    os.replace(tmp, dest)
    return dest


def ensure_node(quiet=False):
    """返回 node.exe 路径；没有就下载。失败返回 None（调用方会用系统 Node 兜底）。"""
    if have_node():
        return NODE
    try:
        if not quiet:
            print("没找到内置的 node.exe，正在自动下载…")
        return download()
    except Exception as e:
        if not quiet:
            print("自动下载失败：%s" % e)
            print("可以手动下载后放到：%s" % NODE)
            print("  %s" % URL)
        return None


if __name__ == "__main__":
    p = ensure_node()
    if p:
        print("就绪：%s（%.1f MB）" % (p, os.path.getsize(p) / 1048576.0))
    else:
        sys.exit(1)
