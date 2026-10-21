"""弹幕面板：正方形无边框窗口，贴在目标窗口左侧/右侧（也可自由拖动）。

每条弹幕显示：头像 · 昵称（B 站原色 + 头衔标）· 时间 · 正文；
礼物/上舰/进场等事件单独一行高亮。

用法：
  python danmu.py                              # 按 config.json 的 danmu 段来
  python danmu.py --host "哔哩哔哩"             # 吸附到标题含「哔哩哔哩」的窗口
  python danmu.py --side left                  # 贴左侧（默认右侧）
  python danmu.py --screen 1 --host ""         # 不跟窗口，投到第 2 块屏幕
  python danmu.py --app PotPlayerMini64.exe    # 只吸附该进程的窗口
  python danmu.py --test                       # 造弹幕看效果
  python danmu.py --emit "文本"                 # 往运行中的面板推一条

面板内：左键拖动=改位置（吸附后仍可拖，拖到窗口外就固定在那）；右键/双击关闭；
Ctrl+Alt+D 显示/隐藏；Esc 或 Ctrl+Alt+Q 关闭。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import socket
import threading
import time
import tkinter as tk

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

SWP_NOSIZE, SWP_NOACTIVATE = 0x0001, 0x0010
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _title_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid_of(hwnd):
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return p.value


def exe_of(hwnd):
    """进程可执行文件名（不含路径），取不到返回空串。"""
    path = ctypes.create_unicode_buffer(520)
    size = wt.DWORD(520)
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, _pid_of(hwnd))
    try:
        if not h:
            return ""
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
            return ""
    finally:
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
    return os.path.basename(path.value)


def find_window(substr, app=""):
    """按标题子串（可选再加进程名）找可见且未最小化的窗口，返回 hwnd。"""
    want = (app or "").lower()
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        title = _title_of(hwnd)
        if substr.lower() in title.lower() and (not want or want in exe_of(hwnd).lower()):
            hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def monitor_rect(index=0):
    """第 index 块显示器的矩形 (left, top, right, bottom)，按左→右排序。"""
    mons = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    def cb(hmon, hdc, lprc, data):
        mons.append(hmon)
        return True

    user32.EnumDisplayMonitors(None, None, cb, 0)
    if not mons:
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

    def rect(m):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(m, ctypes.byref(mi))
        r = mi.rcMonitor
        return (r.left, r.top, r.right, r.bottom)

    rects = sorted((rect(m) for m in mons), key=lambda r: (r[0], r[1]))
    return rects[min(max(index, 0), len(rects) - 1)]


MOD_NAMES = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004,
             "win": 0x0008, "cmd": 0x0008}
WM_HOTKEY, MOD_NOREPEAT = 0x0312, 0x4000


class _Hotkey:
    """全局热键（RegisterHotKey），热键消息在 tkinter 事件循环里取。"""

    _id = 0x9001

    def __init__(self, spec, callback, root):
        vk, mods = self._parse(spec)
        user32.RegisterHotKey(None, self._id, mods | MOD_NOREPEAT, vk)
        self.cb, self.root = callback, root
        self._id = _Hotkey._id
        _Hotkey._id += 1
        root.after(200, self._poll)

    @staticmethod
    def _parse(spec):
        vk, mods = 0, 0
        for part in str(spec).lower().replace(" ", "").split("+"):
            if part in MOD_NAMES:
                mods |= MOD_NAMES[part]
            elif len(part) == 1 and part.isalnum():
                vk = ord(part.upper())
            elif part.startswith("f") and part[1:].isdigit():
                vk = 0x70 + int(part[1:]) - 1
            else:
                raise ValueError(spec)
        if not vk:
            raise ValueError(spec)
        return vk, mods

    def _poll(self):
        try:
            m = wt.MSG()
            if user32.PeekMessageW(ctypes.byref(m), None, WM_HOTKEY, WM_HOTKEY, 1):
                if m.message == WM_HOTKEY and m.wParam == self._id:
                    self.cb()
        except Exception:
            pass
        try:
            self.root.after(120, self._poll)
        except Exception:
            pass


# ---------------------------------------------------------------- 弹幕面板
class DanmuPanel:
    """正方形弹幕面板。默认贴目标窗口右侧，边长=目标窗口高度。"""

    def __init__(self, cfg, host_title="", test=False, port=0, app="", screen=None):
        self.cfg = cfg
        self.host_title = host_title
        self.app = app
        self.port = port
        self.test = test
        self.hidden = False
        self.host = None
        self.dragging = False
        self.moved = False          # 用户拖过之后就不再自动吸附
        self._dxy = (0, 0)

        self.bg = cfg.get("bg", "#14151b")
        self.rows = []              # 消息行 Frame
        self.avatars = {}
        self.max_msgs = int(cfg.get("max_msgs", 30))
        self.side = cfg.get("side", "right")

        self.root = r = tk.Tk()
        r.title("弹幕面板")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        try:
            r.attributes("-alpha", float(cfg.get("alpha", 0.92)))
        except Exception:
            pass
        r.configure(bg=self.bg)

        self._build_header()
        self.box = tk.Frame(r, bg=self.bg)
        self.box.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.box, bg=self.bg, highlightthickness=0, bd=0)
        self.sb = tk.Scrollbar(self.box, orient="vertical", command=self.canvas.yview,
                               width=8, bg="#2b2d38", troughcolor=self.bg, bd=0,
                               activebackground="#3b3d4a")
        self.inner = tk.Frame(self.canvas, bg=self.bg)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

        self._place()
        r.update_idletasks()
        self.hwnd = user32.GetParent(r.winfo_id())

        for w in (r, self.header, self.canvas, self.inner):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", lambda e: r.destroy())
            w.bind("<Double-Button-1>", lambda e: r.destroy())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.inner.bind("<MouseWheel>", self._wheel)

        self._hk_toggle = self._hk_quit = None
        for spec, fn in ((cfg.get("toggle_hotkey", "ctrl+alt+d"), self.toggle),
                         (cfg.get("quit_hotkey", "ctrl+alt+q"), r.destroy)):
            if not spec:
                continue
            try:
                hk = _Hotkey(spec, fn, r)
                if fn == self.toggle:
                    self._hk_toggle = hk
                else:
                    self._hk_quit = hk
            except Exception:
                pass

        if port:
            threading.Thread(target=self._serve, args=(port,), daemon=True).start()
        if test:
            threading.Thread(target=self._demo, daemon=True).start()
        r.after(600, self._follow)
        r.after(300, self._refresh_scroll)

    # ---------- 外观 ----------
    def _build_header(self):
        self.header = h = tk.Frame(self.root, bg="#1d1f27", height=26)
        h.pack(side="top", fill="x")
        h.pack_propagate(False)
        self.dot = tk.Label(h, text="●", fg="#f55", bg="#1d1f27", font=("Segoe UI", 11))
        self.dot.pack(side="left", padx=(8, 2))
        self.title_lbl = tk.Label(h, text="弹幕", fg="#e8e8f0", bg="#1d1f27",
                                  font=("Microsoft YaHei UI", 9), anchor="w")
        self.title_lbl.pack(side="left")
        tk.Label(h, text="�� 拖动 / 右键关闭", fg="#6b6f80", bg="#1d1f27",
                 font=("Microsoft YaHei UI", 8)).pack(side="right", padx=6)

    def set_title(self, text, ok=True):
        try:
            self.title_lbl.config(text=text)
            self.dot.config(fg="#3fb950" if ok else "#f55")
        except Exception:
            pass

    # ---------- 位置 ----------
    def _place(self):
        """初始定位：有目标窗口就贴它（边长=窗口高度），否则固定在屏幕上。"""
        cfg, side = self.cfg, self.side
        host = find_window(self.host_title, self.app) if self.host_title else None
        if host:
            self.host = host
            _, top, _, bottom = self._host_rect(host)
            size = max(bottom - top, 320)
            x = self._host_x(host, size, side)
            self.root.geometry("%dx%d+%d+%d" % (size, size, x, top))
            return
        mleft, mtop, mright, _ = monitor_rect(self.cfg.get("screen", 0)
                                               if not self.host_title else
                                               self._monitor_of_host())
        size = int(cfg.get("size", 0) or 0) or min(mright - mleft, 520)
        x = mright - size - 40 if side == "right" else mleft + 40
        self.root.geometry("%dx%d+%d+%d" % (size, size, x, mtop + 80))

    def _monitor_of_host(self):
        return 0

    @staticmethod
    def _host_rect(hwnd):
        rc = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return rc.left, rc.top, rc.right, rc.bottom

    def _host_x(self, hwnd, size, side):
        left, _, right, _ = self._host_rect(hwnd)
        return right - size if side == "right" else left

    def _follow(self):
        try:
            if self.host_title and not self.moved and not self.dragging and \
                    self.cfg.get("attach", "side") != "free":
                if self.host is None or not user32.IsWindow(self.host):
                    self.host = find_window(self.host_title, self.app)
                if self.host:
                    left, top, right, bottom = self._host_rect(self.host)
                    size = max(bottom - top, 320)
                    x = self._host_x(self.host, size, self.side)
                    if not self.hidden:
                        self.root.geometry("%dx%d+%d+%d" % (size, size, x, top))
                elif self.cfg.get("auto_hide", True):
                    self.root.withdraw()
        except Exception:
            pass
        try:
            self.root.after(600, self._follow)
        except Exception:
            pass

    def _drag_start(self, e):
        self.dragging = True
        self._dxy = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e):
        if not self.moved:
            self.root.deiconify()
        self.moved = True
        dx, dy = self._dxy
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))

    def _drag_end(self, e):
        self.dragging = False
        # 双击吸附回去
        if self.moved and self.cfg.get("snap_back_on_double_click", True):
            pass

    def toggle(self):
        self.hidden = not self.hidden
        try:
            if self.hidden:
                self.root.withdraw()
            else:
                self.root.deiconify()
        except Exception:
            pass

    # ---------- 滚动 ----------
    def _on_inner(self, e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._refresh_scroll()

    def _on_canvas(self, e=None):
        self.canvas.itemconfigure(self.inner_id, width=self.canvas.winfo_width())

    def _wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def _refresh_scroll(self):
        """在底部时才自动跟随最新；往上翻过就停住不跳。"""
        try:
            self.canvas.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            first, last = self.canvas.yview()
            if self.cfg.get("auto_scroll", True):
                self.canvas.yview_moveto(1.0)
        except Exception:
            pass

    # ---------- 外部输入 ----------
    def _serve(self, port):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            self.set_title("端口 %d 被占用" % port, ok=False)
            return
        s.listen(16)
        while True:
            try:
                c, _ = s.accept()
                with c:
                    for line in c.makefile("rb"):
                        if line.strip():
                            m = json.loads(line.decode("utf-8"))
                            self.root.after(0, self.push, m)
            except Exception:
                pass

    def _demo(self):
        users = [("小明", "ffffff", "舰长", 3, 26), ("路人甲", "5fd7ff", "粉丝团", 12, 14),
                 ("大老板", "ffd166", "提督", 2, 30), ("新来的", "ffffff", "", 0, 3)]
        lines = ["点一首《晴天》", "666666", "主播唱得真好", "前方高能", "awsl",
                 "【点歌】起风了 - 买辣椒也用券", "这首歌我也会", "弹幕机上线",
                 "这个面板可以自由拖动", "多屏也能用"]
        n = 0
        while True:
            u, color, medal, guard, lvl = random.choice(users)
            n += 1
            if n % 7 == 0:
                self.root.after(0, self.push, {
                    "kind": "event", "text": "🎁 %s 投喂 小心心×3" % u,
                    "user": u, "color": "ffd166"})
            else:
                self.root.after(0, self.push, {
                    "kind": "chat", "user": u, "color": color, "text": random.choice(lines),
                    "medal": medal, "guard": guard, "is_admin": lvl > 25,
                    "level": lvl, "time": time.strftime("%H:%M:%S"),
                    "face": ""})
            time.sleep(random.uniform(0.6, 1.8))

    def push(self, m):
        kind = m.get("kind", "chat")
        row = tk.Frame(self.inner, bg=self.bg, padx=6, pady=3)
        row.pack(fill="x", anchor="w")

        # 头像（圆形很难做，用等宽方块 + 首字兜底）
        face = m.get("face") or ""
        av = tk.Label(row, text=(m.get("user") or "?")[:1], width=2, height=1,
                      bg="#2b2d38", fg="#cfd3e0", font=("Microsoft YaHei UI", 9),
                      bd=0)
        av.pack(side="left", anchor="n", padx=(0, 6))
        if face:
            self._load_avatar(av, face)

        body = tk.Frame(row, bg=self.bg)
        body.pack(side="left", fill="x", expand=True)

        head = tk.Frame(body, bg=self.bg)
        head.pack(fill="x", anchor="w")
        color = "#" + (m.get("color") or "ffffff")
        if kind == "event":
            color = "#ffd166"
        tk.Label(head, text=m.get("user") or "", fg=color, bg=self.bg,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")

        guard = m.get("guard") or 0
        if guard:
            gname = {1: "总督", 2: "提督", 3: "舰长"}.get(guard, "舰长")
            gbg = {1: "#e08ee8", 2: "#b57ce0", 3: "#6fa8dc"}.get(guard, "#6fa8dc")
            tk.Label(head, text=" " + gname + " ", bg=bg_of(gbg), fg="#fff",
                     font=("Microsoft YaHei UI", 7, "bold")).pack(side="left", padx=3)
        medal = m.get("medal") or ""
        if medal:
            tk.Label(head, text=" %s%d " % (medal, m.get("medal_level") or 0),
                     bg="#3a2f14", fg="#e3b341",
                     font=("Microsoft YaHei UI", 7)).pack(side="left", padx=2)
        if m.get("is_admin"):
            tk.Label(head, text=" 房管 ", bg="#7a1f1f", fg="#ffd0d0",
                     font=("Microsoft YaHei UI", 7, "bold")).pack(side="left", padx=2)
        if m.get("is_vip"):
            tk.Label(head, text=" 月费 ", bg="#2d2a5c", fg="#c9c1ff",
                     font=("Microsoft YaHei UI", 7)).pack(side="left", padx=2)
        if m.get("level"):
            tk.Label(head, text=" UL%d" % m["level"], bg=self.bg, fg="#5c6070",
                     font=("Microsoft YaHei UI", 7)).pack(side="left", padx=2)
        tk.Label(head, text=" " + (m.get("time") or ""), bg=self.bg, fg="#565a68",
                 font=("Consolas", 8)).pack(side="right")

        text = m.get("text") or ""
        fg = "#e6e8f0" if kind == "chat" else "#ffd166"
        lbl = tk.Label(body, text=text, fg=fg, bg=self.bg, justify="left", anchor="w",
                       wraplength=max(self.root.winfo_width() - 80, 200),
                       font=("Microsoft YaHei UI", int(self.cfg.get("font_size", 12))))
        lbl.pack(fill="x", anchor="w")
        for w in (row, body, head):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<MouseWheel>", self._wheel)

        self.rows.append(row)
        while len(self.rows) > self.max_msgs:
            try:
                self.rows.pop(0).destroy()
            except Exception:
                pass
        self._refresh_scroll()

    def _load_avatar(self, lbl, url):
        """异步取头像（带内存缓存 + 磁盘缓存），失败就保持首字方块。"""
        try:
            img = self.avatars.get(url)
            if img:
                lbl.config(image=img, text="", width=32, height=32)
                lbl.image = img
                return
        except Exception:
            pass

        def work():
            try:
                path = avatar_cache(url)
                if not path:
                    return
                self.root.after(0, apply, path)
            except Exception:
                pass

        def apply(path):
            try:
                img = tk.PhotoImage(file=path)
                w = img.width()
                if w > 32:
                    img = img.subsample(max(1, (w + 15) // 32))
                self.avatars[url] = img
                lbl.config(image=img, text="", width=32, height=32)
                lbl.image = img
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def show(self):
        self.root.mainloop()


def bg_of(hexstr):
    return "#" + hexstr.lstrip("#")


def avatar_cache(url, cache_dir=None):
    """下载头像到本地缓存（tkinter 只认 gif/png 文件，B 站头像是 jpg，
    浏览器能显示但 tk 不能 —— 所以先转成 png 需要 Pillow；没有 Pillow 就放弃）。"""
    import hashlib
    d = cache_dir or os.path.join(os.path.expanduser("~"), ".danmuji", "avatars")
    try:
        os.makedirs(d, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()
        out = os.path.join(d, key + ".png")
        if os.path.exists(out):
            return out
        import requests
        u = url if url.startswith("http") else "https:" + url
        u += "" if "?" in u else "@64w_64h.png"
        r = requests.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://live.bilibili.com/"})
        if r.status_code != 200:
            return ""
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            im.thumbnail((40, 40))
            im.save(out, "PNG")
        except ImportError:
            if r.content[:8] != b"\x89PNG\r\n\x1a\n":
                return ""   # 没有 Pillow 就没法转 png
            with open(out, "wb") as f:
                f.write(r.content)
        return out
    except Exception:
        return ""


def emit(port, text, color=None, **kw):
    m = {"text": text, "color": color}
    m.update(kw)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
        c.sendall((json.dumps(m, ensure_ascii=False) + "\n").encode())


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="吸附的窗口标题子串（留空=固定在屏幕上）")
    ap.add_argument("--app", default=None, help="限定进程名，如 PotPlayerMini64.exe")
    ap.add_argument("--side", default=None, choices=["left", "right"], help="贴窗口左边还是右边")
    ap.add_argument("--screen", type=int, default=None, help="不跟窗口时投到第几块屏幕（0 起）")
    ap.add_argument("--free", action="store_true", help="不跟随窗口")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--emit", default="")
    ap.add_argument("--cfg", default=os.path.join(here, "config.json"))
    a = ap.parse_args()
    cfg = {}
    try:
        with open(a.cfg, encoding="utf-8") as f:
            cfg = json.load(f).get("danmu", {})
    except Exception:
        pass
    port = a.port or cfg.get("port", 9701)
    if a.emit:
        emit(port, a.emit)
        return
    if a.free:
        cfg["attach"] = "free"
    host = cfg.get("host_title", "") if a.host is None else a.host
    if a.side:
        cfg["side"] = a.side
    DanmuPanel(cfg, host, a.test, port,
               app=cfg.get("app", "") if a.app is None else a.app,
               screen=a.screen).show()


if __name__ == "__main__":
    main()
