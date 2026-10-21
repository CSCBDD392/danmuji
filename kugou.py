"""酷狗网页版：搜索（公开接口）+ 在真实浏览器页签里播放（CDP）。

设计取舍（踩过的坑写在这）：
  - 搜索用公开接口 songsearch.kugou.com/song_search_v2，拿 hash/album_id/mixsongid。
  - 取歌地址接口 play/songinfo 对无登录态/IP 有风控（err_code 30020），
    自己拼签名、换域名、换参数都不行；而网页版播放器在无登录态下也播不了。
  - 因此播放交给「酷狗网页版页签」：打开混音页，点播放按钮；
    若页面拿不到地址，就交给用户点一下（页面会弹登录/QQ 或手机号登录）。
    所有 cookie 都存在独立 profile 里，登录一次长期有效。
  - 歌曲链接不落地保存，只用页面内的 <audio> 播放，避免下载/传播音乐文件。
"""
import json
import logging
import os
import re
import subprocess
import time

import requests

import cdp

log = logging.getLogger("kugou")

SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
MIXSONG_URL = "https://www.kugou.com/mixsong/%s.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _clean(s):
    return re.sub(r"</?em>|<\d+>|</?\w+>", "", s or "").replace("&amp;", "&").strip()


class KugouSearch:
    def __init__(self, timeout=8):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def search(self, kw, n=8):
        r = self.s.get(SEARCH_URL, params={"keyword": kw, "page": 1, "pagesize": n,
                                           "platform": "WebFilter", "tag": 0, "filter": 2},
                       timeout=self.timeout)
        r.raise_for_status()
        lists = (r.json().get("data") or {}).get("lists") or []
        return [{"title": _clean(it.get("SongName")),
                 "artist": _clean(it.get("SingerName")).replace("、", "/"),
                 "duration": it.get("Duration"),
                 "hash": it.get("FileHash"),
                 "album_id": it.get("AlbumID"),
                 "mixsongid": it.get("MixSongID"),
                 "privilege": it.get("Privilege"),
                 "pay": it.get("PayType"),
                 "album": _clean(it.get("AlbumName"))} for it in lists[:n]]

    def best(self, title, artist=""):
        hits = self.search("%s %s" % (title, artist))
        if not hits:
            return None

        def score(h):
            s = 0
            if title.lower() in h["title"].lower():
                s += 10
            if artist and artist.lower() in h["artist"].lower():
                s += 5
            if re.search(r"live|伴奏|dj|翻唱|remix|片段", h["title"] + h["album"], re.I):
                s -= 3
            if h.get("privilege") in (0, 10):   # 无版权/付费：仍然返回，交给页面兜底
                s -= 1
            return s

        return max(hits, key=score)  # 同分取第一个（接口按相关度排序）


class Browser:
    """通过 CDP 端口找/开浏览器页签；用自带的 cdp.py 发 Runtime.evaluate。"""

    def __init__(self, kugou_cfg=None):
        c = kugou_cfg or {}
        self.exe = os.path.expandvars(c.get("browser_exe") or "")
        self.port = int(c.get("cdp_port", 9333))
        self.user_dir = os.path.expanduser(c.get("user_dir", "~/.danmuji/kugou-profile"))
        self.url = c.get("page_url", "https://www.kugou.com/")
        self._tab = None

    def _tabs(self):
        try:
            return requests.get("http://127.0.0.1:%d/json/list" % self.port, timeout=2).json()
        except Exception:
            return []

    def tab(self):
        if self._tab and any(t["id"] == self._tab["id"] for t in self._tabs()):
            return self._tab
        tabs = [t for t in self._tabs()
                if t.get("type") == "page" and "kugou.com" in t.get("url", "")]
        if not tabs:
            if not self._tabs():
                self._launch()
            tabs = [requests.put("http://127.0.0.1:%d/json/new?%s" % (self.port, self.url),
                                 timeout=10).json()]
        self._tab = tabs[0]
        return self._tab

    def open(self, url):
        if not self._tabs():
            self._launch()
        return requests.put("http://127.0.0.1:%d/json/new?%s" % (self.port, url),
                            timeout=10).json()

    def close(self, tab):
        try:
            requests.get("http://127.0.0.1:%d/json/close/%s" % (self.port, tab["id"]), timeout=3)
        except Exception:
            pass

    def focus(self, tab):
        try:
            requests.get("http://127.0.0.1:%d/json/activate/%s" % (self.port, tab["id"]),
                         timeout=3)
        except Exception:
            pass

    def _launch(self):
        exe = self.exe or self._find_browser()
        if not exe:
            raise RuntimeError("没找到 Edge/Chrome，请在 config.json 里填 kugou.browser_exe")
        os.makedirs(self.user_dir, exist_ok=True)
        log.info("启动浏览器（调试端口 %s，用户数据 %s）", self.port, self.user_dir)
        subprocess.Popen([exe, "--remote-debugging-port=%d" % self.port,
                          "--user-data-dir=" + self.user_dir, "--no-first-run",
                          "--no-default-browser-check", self.url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(75):
            if self._tabs():
                return
            time.sleep(0.2)
        raise RuntimeError("浏览器调试端口 %d 未就绪" % self.port)

    @staticmethod
    def _find_browser():
        for p in (r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
                  r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                  r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                  r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                  r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"):
            p = os.path.expandvars(p)
            if os.path.exists(p):
                return p
        return ""


class Page:
    """酷狗页面操作门面；JS 主体在 page.js（%name% 占位注入参数）。"""

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.br = Browser(cfg)
        self.timeout = float(self.cfg.get("page_timeout", 20))
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.js")
        with open(js_path, encoding="utf-8") as f:
            self.js = json.load(f)

    def _ws(self):
        return self.br.tab()["webSocketDebuggerUrl"]

    def eval(self, name, **kw):
        expr = self.js[name]
        for k, v in kw.items():
            expr = expr.replace("%" + k + "%", json.dumps(v, ensure_ascii=False))
        return cdp.evaluate(self._ws(), expr, timeout=self.timeout)

    def eval_raw(self, expr, timeout=None):
        return cdp.evaluate(self._ws(), expr, timeout=timeout or self.timeout)

    def enter_search(self, kw):
        """像真人一样在搜索框里打字，再发一个真 Enter（酷狗只认真键盘事件）。"""
        ws = self._ws()
        if not cdp.evaluate(ws, self.js["focus"]):
            return False
        for ch in kw:
            cdp._call(ws, "Input.insertText", {"text": ch})
        cdp.key(ws, "Enter", "Enter", 13)
        return True

    def rows(self):
        return self.eval("rows") or []

    def playing(self):
        return self.eval("playing")

    def pause(self):
        return self.eval("ctrl", cmd="pause")

    # —— 播放：打开混音页 → 点播放 → 等出声；不出声就把页签让给用户 ——
    def play(self, song, focus_if_stuck=True, wait=9.0):
        mix = str(song.get("mixsongid") or "")
        url = MIXSONG_URL % mix if mix else self._search_url(song)
        tab = self.br.open(url)
        ws = tab["webSocketDebuggerUrl"]
        try:
            cdp.navigate_ctx(ws, url, wait=700)
        except Exception:
            pass
        got = self._wait_src(ws, wait)
        if got.get("url") and got.get("playing"):
            return {"ok": True, "how": "autoplay", "info": got.get("info")}
        # 还没放出来 → 点页面上的播放按钮（有些情况需要一次手势）
        try:
            clicked = cdp.evaluate(ws, self.js["click"], timeout=10)
        except Exception:
            clicked = None
        got = self._wait_src(ws, 5)
        if got.get("url") and got.get("playing"):
            return {"ok": True, "how": "click", "info": got.get("info"), "click": clicked}
        if focus_if_stuck:
            self.br.focus(tab)  # 把页签切到前台，让用户点一下登录/播放
        return {"ok": bool(got.get("url")), "how": "stuck", "info": got.get("info"),
                "err": got.get("err"), "tab": tab, "url": url}

    def _wait_src(self, ws, wait):
        end = time.time() + wait
        got = {}
        while time.time() < end:
            try:
                got = cdp.evaluate(ws, self.js["src"], timeout=5) or {}
            except Exception:
                got = {}
            if got.get("playing"):
                return got
            if got.get("url"):
                return got
            time.sleep(0.4)
        return got

    @staticmethod
    def _search_url(song):
        kw = ("%s %s" % (song.get("title", ""), song.get("artist", ""))).strip()
        return search_page_url(kw)


def search_page_url(kw):
    return ("https://www.kugou.com/yy/html/search.html#searchType=song&searchKeyWord="
            + requests.utils.quote(kw))
