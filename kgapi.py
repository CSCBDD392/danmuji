"""内置酷狗音乐 API 引擎的托管：探测、自动拉起、健康检查。

为什么要宿主托管：
  这个 API 是 Express 服务（默认 127.0.0.1:4000）。它只服务本机，不需要对公网
  暴露，也不需要 Cloudflare 隧道——点歌机自己就在同一台机器上。

三条路径（按顺序尝试）：
  1. config.kugou_api.base_url 指向的服务已经在跑 → 直接用
  2. 找到 vendor/kugou-api 目录 + 机器上已装的 Node → 自己拉起来，退出时关掉
  3. 都找不到 → 返回 None，播放器退回「网页版浏览器」后端
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("kgapi")

HERE = os.path.dirname(os.path.abspath(__file__))

# 曾经踩过的坑：这个上游项目 generate_simulate.js 里漏了 generateWebGLHash 的
# import，导致每次触发 SSA 二次验证都 502（"本次请求需要验证" 被抛成空对象）。
# 第一次运行时顺手把这一行补上，用家就不会看到莫名其妙的 502。
_FIX_OLD = "const { randomString } = require('./util');"
_FIX_NEW = "const { randomString, generateWebGLHash } = require('./util');"


def _port_open(host, port, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _node_exe():
    """内置的 node.exe 优先 —— 用户机器上不需要装 Node。

    内置的那个是大文件，放 GitHub Release，第一次用会由 tools/get_runtime.py
    自动下载；下不了就退回系统装好的 Node。
    """
    bundled = os.path.join(HERE, "vendor", "kugou-api", "node.exe")
    if os.path.isfile(bundled):
        return bundled
    for p in (r"C:\Program Files\nodejs\node.exe",
              r"C:\Program Files (x86)\nodejs\node.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe")):
        if os.path.isfile(p):
            return p
    for name in ("node.exe", "node"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            f = os.path.join(d, name)
            if os.path.isfile(f):
                return f
    return None


def ensure_runtime():
    """确认内置 node.exe 在；不在就下。返回路径或 None。"""
    if _node_exe():
        return _node_exe()
    try:
        from tools import get_runtime
    except Exception:
        try:
            sys.path.insert(0, os.path.join(HERE, "tools"))
            import get_runtime
        except Exception as e:
            log.debug("加载 get_runtime 失败: %s", e)
            return None
    p = get_runtime.ensure_node()
    return p


def _node_is_bundled(path):
    return bool(path) and os.path.normcase(path).startswith(
        os.path.normcase(os.path.join(HERE, "vendor")))


def _patch_upstream(root):
    """补上上游 util/generate_simulate.js 缺失的 generateWebGLHash 导入。"""
    f = os.path.join(root, "util", "generate_simulate.js")
    if not os.path.isfile(f):
        return
    try:
        src = open(f, "rb").read()
        if _FIX_NEW.encode() in src or _FIX_OLD.encode() not in src:
            return
        open(f, "wb").write(src.replace(_FIX_OLD.encode(), _FIX_NEW.encode(), 1))
        log.info("已修复 kugou-api 的 generateWebGLHash 导入")
    except Exception as e:                       # 只读目录就别管了
        log.debug("补丁失败（忽略）: %s", e)


class KugouEngine:
    """管理「酷狗 API 服务」的生命周期。"""

    def __init__(self, cfg):
        c = (cfg or {}).get("kugou_api") or {}
        self._cfg = cfg
        self.enabled = c.get("enabled", True)
        self.host = c.get("host", "127.0.0.1")
        self.port = int(c.get("port", 4000))
        self.root = os.path.expanduser(c.get("root") or os.path.join(HERE, "vendor", "kugou-api"))
        self.base_url = c.get("base_url") or "http://%s:%d" % (self.host, self.port)
        self.extra_args = list(c.get("args") or [])
        self.start_timeout = float(c.get("start_timeout", 25))
        self.dfid = c.get("dfid") or ""
        self.uuid = c.get("uuid") or ""
        self.token = c.get("token") or ""
        self.userid = c.get("userid") or ""
        self.cookie = c.get("cookie") or ""
        self._proc = None
        self._lock = threading.Lock()
        self._ready = False
        self._blocked = {}          # hash -> 冷却结束时间
        self._backoff = float(c.get("retry_backoff_sec", 90))
        self._last_dfid = ""

    # ---------------- 基础 ----------------
    @property
    def pid(self):
        return self._proc.pid if self._proc else 0

    def cookie_header(self):
        if self.cookie:
            return self.cookie
        parts = []
        if self.dfid:
            parts.append("dfid=" + self.dfid)
        if self.userid:
            parts.append("userid=" + str(self.userid))
        if self.token:
            parts.append("token=" + str(self.token))
        return "&".join(parts)

    def get(self, path, timeout=25):
        url = self.base_url.rstrip("/") + path
        req = urllib.request.Request(url)
        ck = self.cookie_header()
        if ck:
            req.add_header("Cookie", ck)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")

    # ---------------- 启动/停止 ----------------
    def _looks_alive(self):
        if not _port_open(self.host, self.port):
            return False
        try:
            code, body = self.get("/server/now", timeout=4)
            return code == 200 and "status" in body
        except Exception:
            try:
                code, body = self.get("/search?keywords=test&page=1&pagesize=1", timeout=6)
                return code == 200
            except Exception:
                return False

    def alive(self):
        """给聚合层用：服务在不在。不负责拉起，拉起的活儿归 ensure()。"""
        return self._looks_alive()

    def _persist(self):
        """把 dfid/token 落盘，下次启动别再重新注册（每次注册都会被风控盯上）。"""
        cfg = self._cfg
        if not isinstance(cfg, dict):
            return
        kc = cfg.setdefault("kugou_api", {})
        kc["dfid"] = self.dfid
        kc["uuid"] = self.uuid
        if self.token:
            kc["token"] = self.token
        if self.userid:
            kc["userid"] = self.userid
        try:
            with open(os.path.join(HERE, "config.local.json"), "w",
                      encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("保存设备指纹失败：%s", e)

    def ensure_dfid(self, force=False):
        """确保有一个可用的设备指纹（dfid）。

        酷狗会对没登记过的设备指纹返回 20028「本次请求需要验证」，连免费歌都
        放不了。所以首次运行先调 /register/dev 领一个，存进配置里长期复用。

        上游这个接口把设备 uuid 当必填（没给就 uuid=undefined，服务端直接
        返回空 data），所以这里必须带一个稳定的 uuid 上去。
        """
        if self.dfid and not force:
            return self.dfid
        if not self.uuid:
            # 一个机器一个 uuid，别每次变（换了又得重新登记）
            import uuid as _uuid
            self.uuid = _uuid.uuid4().hex
        try:
            # 上游把 uuid 从 cookie.KUGOU_API_GUID 里取，不给就 uuid=undefined，
            # 服务端会直接返回空 data（{"status":1,"data":[]}）。所以必须带上。
            old_cookie = self.cookie
            self.cookie = "KUGOU_API_GUID=%s" % self.uuid
            try:
                code, body = self.get("/register/dev", timeout=25)
            finally:
                self.cookie = old_cookie
            d = json.loads(body)
            if d.get("status") == 1 and (d.get("data") or {}).get("dfid"):
                self.dfid = d["data"]["dfid"]
                log.info("已注册新设备指纹 dfid=%s", self.dfid)
                self._persist()
                return self.dfid
            log.warning("注册设备指纹失败：%s", str(body)[:120])
        except Exception as e:
            log.warning("注册设备指纹异常：%s", e)
        return self.dfid

    def ensure(self):
        """确保引擎可用；返回 True/False。"""
        if not self.enabled:
            return False
        with self._lock:
            if self._ready and self._proc is not None and self._proc.poll() is None:
                self.ensure_dfid()
                return True
            if self._looks_alive():
                self._ready = True
                log.info("用已有的酷狗 API：%s", self.base_url)
                self.ensure_dfid()
                return True
            if self._proc is not None and self._proc.poll() is None:
                return False
            # 内置 node.exe 缺失时自动下载（只发生一次；下不了就退回系统 Node）
            node = ensure_runtime()
            app = os.path.join(self.root, "app.js")
            if not node or not os.path.isfile(app):
                log.info("没找到内置酷狗 API（node=%s, app=%s）", bool(node), os.path.isfile(app))
                return False
            _patch_upstream(self.root)
            env = dict(os.environ)
            env.setdefault("PORT", str(self.port))
            env.setdefault("platform", "lite")
            env["PKG_CACHE_PATH"] = env.get("PKG_CACHE_PATH") or os.path.join(self.root, ".pkgcache")
            flags = 0
            if sys.platform == "win32":
                flags = 0x08000000 | 0x00000100        # CREATE_NO_WINDOW|STARTF_USESTDHANDLES
            try:
                self._proc = subprocess.Popen(
                    [node, app] + self.extra_args, cwd=self.root, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags)
            except Exception as e:
                log.warning("拉起酷狗 API 失败: %s", e)
                return False
            log.info("已拉起内置酷狗 API (pid=%s) %s", self._proc.pid, self.base_url)
            end = time.time() + self.start_timeout
            while time.time() < end:
                if self._proc.poll() is not None:
                    log.warning("酷狗 API 进程启动即退出，code=%s", self._proc.returncode)
                    self._proc = None
                    return False
                if self._looks_alive():
                    self._ready = True
                    self.ensure_dfid()
                    return True
                time.sleep(0.4)
            log.warning("酷狗 API 启动超时")
            return False

    def stop(self):
        with self._lock:
            p, self._proc, self._ready = self._proc, None, False
        if p is None or p.poll() is not None:
            return
        log.info("关闭内置酷狗 API (pid=%s)", p.pid)
        try:
            p.terminate()
            p.wait(timeout=4)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---------------- 业务接口 ----------------
    def search(self, kw, n=10):
        q = urllib.parse.urlencode({"keywords": kw, "page": 1, "pagesize": n})
        code, body = self.get("/search?" + q)
        if code != 200:
            return []
        try:
            d = json.loads(body)
        except Exception:
            return []
        lists = ((d.get("data") or {}).get("lists")) or []
        out = []
        for it in lists:
            if not it.get("FileHash") or it.get("Type") not in (None, "audio"):
                continue
            name = (it.get("SongName") or it.get("OriSongName")
                    or it.get("FileName") or "")
            artist = it.get("SingerName") or ""
            # FileName 通常是「歌手 - 歌名」，别再拼一遍歌手
            if not it.get("SongName") and not it.get("OriSongName") and artist:
                for sep in (" - ", " – ", "-"):
                    if name.startswith(artist + sep):
                        name = name[len(artist) + len(sep):]
                        break
            out.append({
                "title": name,
                "artist": artist,
                "album": it.get("AlbumName") or "",
                "hash": it.get("FileHash"),
                "album_id": it.get("AlbumID") or "",
                "duration": int(it.get("Duration") or 0),
                "pay": it.get("PayType"),
                "privilege": it.get("AlbumPrivilege") or it.get("Privilege"),
                "filesize": int(it.get("FileSize") or 0),
            })
        return out

    def song_url(self, hash_, album_id="", free_part=1):
        """返回 (url, 说明)。失败时 url 为 None。

        酷狗对同一设备的取址请求有频率限制，连续打会一直返回 20028
        「本次请求需要验证」。所以失败要退避，别把冷却期白白耗光。
        """
        now = time.time()
        hit = self._blocked.get(hash_)
        if hit and now < hit:
            return None, "太频繁了，%d 秒后再试" % int(hit - now)
        q = {"hash": hash_}
        if album_id:
            q["album_id"] = album_id
        if free_part:
            q["free_part"] = 1
        path = "/song/url?" + urllib.parse.urlencode(q)
        try:
            code, body = self.get(path, timeout=30)
        except Exception as e:
            return None, "请求失败: %s" % e
        if code != 200:
            if "20028" in (body or "") or "验证" in (body or ""):
                self._blocked[hash_] = now + self._backoff
                return None, "本次请求需要验证（稍后重试或登录酷狗）"
            return None, (body or "")[:120]
        try:
            d = json.loads(body)
        except Exception:
            return None, "返回不是 JSON"
        if d.get("status") == 1:
            u = d.get("url") or d.get("backupUrl") or []
            if isinstance(u, list):
                u = u[0] if u else None
            if u:
                self._blocked.pop(hash_, None)
                return u, ""
            return None, "无可用地址"
        msg = str(d.get("error") or d.get("msg") or "无可用地址")
        if "验证" in msg or "登录" in msg:
            self._blocked[hash_] = now + self._backoff
        return None, msg

    # ---------------- 登录（扫码） ----------------
    def qr_create(self):
        code, body = self.get("/login/qr/create", timeout=20)
        try:
            d = json.loads(body)
        except Exception:
            return None
        q = d.get("data") or {}
        return q

    def qr_check(self, key):
        q = urllib.parse.urlencode({"key": key})
        try:
            code, body = self.get("/login/qr/check?" + q, timeout=20)
            return json.loads(body)
        except Exception:
            return None
