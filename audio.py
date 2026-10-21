"""本地音频播放：优先 winsound（系统自带、无依赖），失败退回系统默认播放器。"""
import logging
import os
import subprocess
import sys
import threading
import time

log = logging.getLogger("audio")


class AudioPlayer:
    """下载缓存 + 播放。

    之所以要自己下载：酷狗直链带 ~3 小时过期的 token，而且部分 CDN 会检查
    Referer。先落到本地缓存再播，既稳又不会因为切歌断流。

    winsound 对 MP3 的支持只在 Windows 自带解码器可用时有效；放不了就退回
    os.startfile（用系统默认播放器），并给出提示。
    """

    def __init__(self, cfg=None):
        c = cfg or {}
        self.cache_dir = os.path.expanduser(
            c.get("cache_dir") or os.path.join("~", ".danmuji", "cache"))
        os.makedirs(self.cache_dir, exist_ok=True)
        self.volume = c.get("volume")
        self._stop = threading.Event()
        self._proc = None
        self._timer = None

    # ---------------- 下载 ----------------
    def fetch(self, url, name="song", referer="https://www.kugou.com/"):
        import urllib.request
        ext = ".mp3"
        low = url.lower()
        for e in (".m4a", ".flac", ".ogg", ".wav"):
            if e in low:
                ext = e
                break
        safe = "".join(ch for ch in (name or "song") if ch not in '\\/:*?"<>|').strip()[:60] or "song"
        path = os.path.join(self.cache_dir, "%s_%d%s" % (safe, int(time.time() * 1000) % 100000, ext))
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kugou/11430")
        req.add_header("Referer", referer)
        tmp = path + ".part"
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            total = 0
            while True:
                if self._stop.is_set():
                    raise RuntimeError("已取消")
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        os.replace(tmp, path)
        log.info("已下载 %s (%d KB)", os.path.basename(path), total // 1024)
        return path

    # ---------------- 播放 ----------------
    def play(self, path, block=True):
        """播放本地文件；block=True 时阻塞到播完（或被打断）。返回实际用时。"""
        self._stop.clear()
        t0 = time.time()
        if sys.platform == "win32":
            try:
                import winsound
                if not block:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    return 0
                winsound.PlaySound(path, winsound.SND_FILENAME)   # 同步
                return time.time() - t0
            except Exception as e:
                log.info("winsound 放不了（%s），改用系统播放器", e)
        return self._play_external(path)

    def _play_external(self, path):
        if sys.platform == "win32":
            try:
                os.startfile(path)                     # noqa: S606 用系统默认播放器
                return 0
            except Exception as e:
                log.warning("打开播放器失败: %s", e)
                return 0
        try:
            self._proc = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path])
            return 0
        except Exception as e:
            log.warning("ffplay 不可用: %s", e)
            return 0

    def stop(self):
        self._stop.set()
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
