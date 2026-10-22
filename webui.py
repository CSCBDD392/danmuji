"""WebUI 控制台：浏览器里填配置、一键启动弹幕机 + 点歌机。

设计目标：
  * 只用 Python 标准库（http.server / json / threading），拷到任何装了
    Python 3.7+ 的 Windows 电脑上都能直接跑，不需要 pip install。
  * 默认只监听 127.0.0.1，不对外网暴露。
  * 配置写进 config.local.json（不进 Git），页面里能直接改。

用法：
    python webui.py             # 默认 http://127.0.0.1:9700
    python webui.py --port 8080 --open
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import auth

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_MAIN = os.path.join(HERE, "config.json")
CFG_LOCAL = os.path.join(HERE, "config.local.json")
PAGE = os.path.join(HERE, "webui.html")

DEFAULT_PORT = 9700

AUTH_PAGE = """<!doctype html><meta charset="utf-8">
<title>需要口令 · 弹幕点歌机</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e8eaed;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .box{background:#181b21;padding:36px 40px;border-radius:14px;border:1px solid #262a33;max-width:520px}
 h1{font-size:20px;margin:0 0 14px}p{color:#8b93a3;line-height:1.7;margin:0 0 10px;font-size:14px}
 code{background:#22262e;padding:2px 6px;border-radius:4px;color:#7c9cff}
 .err{color:#ff8a80;margin-top:16px;font-size:13px}
</style>
<div class="box">
 <h1>🔒 需要访问口令</h1>
 <p>这个页面已经从公网暴露，所以要口令才能进。</p>
 <p>口令在服务器上的 <code>config.local.json</code> 里，字段 <code>webui.token</code>。</p>
 <p>在地址后面加上 <code>?t=口令</code> 再打开一次，之后浏览器会记住。</p>
 <div class="err">%s</div>
</div>"""


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _deep_merge(base, over):
    """把 over 合并进 base（就地修改 base 的副本）。"""
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg():
    return _deep_merge(_read_json(CFG_MAIN), _read_json(CFG_LOCAL))


def save_local(patch):
    """只把要改的字段写进 config.local.json，config.json 保持原样。"""
    cur = _read_json(CFG_LOCAL)
    merged = _deep_merge(cur, patch or {})
    tmp = CFG_LOCAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_LOCAL)
    return merged


def _port_up(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


class Runner:
    """管理被拉起来的子进程（弹幕机 / 点歌机 / 面板）。"""

    def __init__(self):
        self.procs = {}
        self.started_at = None
        self.last_error = ""
        self._lock = threading.Lock()

    def _spawn(self, key, argv, cwd=HERE):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008  # DETACHED_PROCESS，不弹黑框
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw)
        with self._lock:
            self.procs[key] = p
        return p

    def running(self):
        with self._lock:
            return {k: (p.poll() is None) for k, p in self.procs.items()}

    def start(self, want_player=True, want_panel=True):
        self.stop()
        py = sys.executable
        if want_panel:
            self._spawn("panel", [py, os.path.join(HERE, "danmu.py")])
            time.sleep(1.2)   # 让面板先把端口占上
        if want_player:
            self._spawn("player", [py, os.path.join(HERE, "main.py")])
        self.started_at = time.time()
        self.last_error = ""
        return self.running()

    def stop(self):
        with self._lock:
            procs = list(self.procs.values())
            self.procs.clear()
        for p in procs:
            try:
                if p.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                       capture_output=True)
                    else:
                        p.terminate()
            except Exception as e:
                self.last_error = str(e)
        self.started_at = None
        return True


RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    _extra_cookie = ""

    def log_message(self, fmt, *args):
        pass    # 安静点，别刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._extra_cookie:
            self.send_header("Set-Cookie", self._extra_cookie)
            self._extra_cookie = ""
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        ok, why, sc = self._auth(u)
        if not ok:
            self._send(401, AUTH_PAGE % {"why": why} if path in ("/", "/index.html", "/webui")
                       else why, "text/html; charset=utf-8")
            return
        if path in ("/", "/index.html", "/webui"):
            if sc:
                self._extra_cookie = sc
            if os.path.exists(PAGE):
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(500, "webui.html 不见了", "text/plain; charset=utf-8")
        elif path == "/api/state":
            cfg = load_cfg()
            self._json({
                "cfg": cfg,
                "running": RUNNER.running(),
                "uptime": int(time.time() - RUNNER.started_at) if RUNNER.started_at else 0,
                "error": RUNNER.last_error,
                "panel_up": _port_up((cfg.get("danmu") or {}).get("port", 9701)),
                "python": sys.executable,
                "has_tk": _has_tk(),
                "token": (cfg.get("webui") or {}).get("token", ""),
            })
        elif path == "/favicon.ico":
            self._send(204, b"")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _auth(self, u):
        """统一鉴权。返回 (ok, 原因, 要种的 cookie)。

        注意：从 CF 隧道进来的请求，源地址一定是 127.0.0.1（cloudflared 在本机），
        直接看 client_address 会把全世界都当成「本机」放行。
        所以优先信 CF-Connecting-IP / X-Forwarded-For。
        """
        q = urllib.parse.parse_qs(u.query)
        cfg = load_cfg()
        tok = (cfg.get("webui") or {}).get("token") or ""
        ip = self._real_ip()
        return auth.check(self.headers, q, ip, tok, self.headers.get("Cookie", ""))

    def _real_ip(self):
        """真实客户端 IP。有 CF 头就用 CF 头，没有才用 TCP 对端。"""
        for hdr in ("CF-Connecting-IP", "X-Real-IP"):
            v = (self.headers.get(hdr) or "").strip()
            if v:
                return v
        xff = (self.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        ok, why, _ = self._auth(u)
        if not ok:
            self._json({"ok": False, "err": why}, 401)
            return
        data = self._body()
        if path == "/api/save":
            try:
                save_local(data.get("patch") or {})
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "err": str(e)}, 500)
        elif path == "/api/start":
            r = RUNNER.start(want_player=data.get("player", True),
                             want_panel=data.get("panel", True))
            self._json({"ok": True, "running": r})
        elif path == "/api/test-danmu":
            room = (data.get("room") or "").strip()
            secs = min(int(data.get("secs") or 12), 30)
            self._json(_test_danmu(room, secs))
        elif path == "/api/stop":
            RUNNER.stop()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "err": "unknown"}, 404)


def _has_tk():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def _test_danmu(room, secs):
    """连一次弹幕，数几秒内收到几条。给 WebUI 的「测试」按钮用。"""
    if not room:
        return {"ok": False, "msg": "没填房间号", "count": 0}
    cfg = load_cfg()
    sessdata = ((cfg.get("live") or {}).get("sessdata") or "").strip()
    if not sessdata:
        return {"ok": False, "count": 0,
                "msg": "没填 SESSDATA —— B 站匿名请求会被 -352 拦掉，填完再测"}

    try:
        import bili
    except Exception as e:
        return {"ok": False, "count": 0, "msg": "加载 bili 失败：%s" % e}
    import time as _t

    got = [0]
    try:
        rid = bili.room_id(room)
    except Exception:
        return {"ok": False, "count": 0, "msg": "房间号看起来不对：%s" % room}

    def cb(kind, payload):
        got[0] += 1

    try:
        th = bili.BiliDanmaku(room, sessdata, cb)
        th.start()
        end = _t.time() + secs
        while _t.time() < end:
            _t.sleep(0.3)
        th.stop()
    except Exception as e:
        return {"ok": False, "count": got[0], "msg": "连接出错：%s" % e}

    if got[0] > 0:
        return {"ok": True, "count": got[0], "msg": ""}
    return {"ok": False, "count": 0,
            "msg": "%d 秒内一条都没收到。SESSDATA 可能过期了，或这个房间没人说话，换个热门房间再试" % secs}


def main():
    ap = argparse.ArgumentParser(description="弹幕点歌机 WebUI")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只给本机访问；想局域网用填 0.0.0.0")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)

    # 暴露到公网必须口令；第一次启动自动生成并写进 config.local.json
    cfg = load_cfg()
    tok = (cfg.get("webui") or {}).get("token") or ""
    if not tok:
        tok = auth.ensure_token(cfg)
        save_local({"webui": {"token": tok}})
        print("已生成访问口令并写入 config.local.json：")
        print("    %s" % tok)
    else:
        print("访问口令：%s" % tok)

    url = "http://%s:%d/" % ("127.0.0.1" if a.host in ("0.0.0.0", "") else a.host, a.port)
    print("WebUI 已启动：%s" % url)
    print("  本机直接打开不用口令：%s" % url)
    print("  外网/局域网打开要带口令：%s?t=%s" % (url, tok))
    print("配置文件：%s" % CFG_LOCAL)
    if a.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭中…")
    finally:
        RUNNER.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
