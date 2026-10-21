"""小控制窗：填直播间、选窗口/屏幕、开始/停止。关掉窗口 = 全部退出。

python main.py --gui
"""
import logging
import os
import threading
import tkinter as tk
from tkinter import ttk

import bili
import main as M

log = logging.getLogger("gui")


class App:
    def __init__(self, cfg, cfg_path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.thread = None
        self.danmaku = None
        self.stats = {}
        live = cfg.setdefault("live", {})
        dm = cfg.setdefault("danmu", {})

        r = self.root = tk.Tk()
        r.title("弹幕点歌机")
        r.geometry("420x300")
        r.resizable(False, False)

        frm = ttk.Frame(r, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="直播间房间号").grid(row=0, column=0, sticky="w")
        self.room = tk.StringVar(value=str(live.get("room", "")))
        ttk.Entry(frm, textvariable=self.room, width=20).grid(row=0, column=1, sticky="w",
                                                             padx=6)
        ttk.Label(frm, text="SESSDATA（可空）").grid(row=1, column=0, sticky="w", pady=4)
        self.sess = tk.StringVar(value=live.get("sessdata", ""))
        ttk.Entry(frm, textvariable=self.sess, width=36, show="*").grid(row=1, column=1,
                                                                      sticky="w", padx=6)

        ttk.Label(frm, text="弹幕面板贴哪个窗口（标题片段）").grid(row=2, column=0, sticky="w")
        self.host = tk.StringVar(value=dm.get("host_title", ""))
        ttk.Entry(frm, textvariable=self.host, width=20).grid(row=2, column=1, sticky="w",
                                                             padx=6)
        ttk.Label(frm, text="贴左边 / 右边").grid(row=3, column=0, sticky="w", pady=4)
        self.side = tk.StringVar(value=dm.get("side", "right"))
        ttk.Combobox(frm, textvariable=self.side, values=["right", "left"], width=8,
                     state="readonly").grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(frm, text="不跟窗口时投到第几块屏").grid(row=4, column=0, sticky="w")
        self.screen = tk.StringVar(value=str(dm.get("screen", 0)))
        ttk.Spinbox(frm, from_=0, to=8, textvariable=self.screen, width=5).grid(
            row=4, column=1, sticky="w", padx=6)

        ttk.Label(frm, text="面板初始大小 / 拉伸方式").grid(row=5, column=0, sticky="w")
        sz = ttk.Frame(frm)
        sz.grid(row=5, column=1, sticky="w", padx=6, pady=2)
        self.size = tk.StringVar(value=str(dm.get("size", 480)))
        ttk.Spinbox(sz, from_=200, to=2000, increment=20, textvariable=self.size,
                    width=6).pack(side="left")
        self.sizemode = tk.StringVar(value=dm.get("size_mode", "square"))
        ttk.Combobox(sz, textvariable=self.sizemode, width=8, state="readonly",
                     values=["square", "free"]).pack(side="left", padx=6)

        ttk.Label(frm, text="音乐源（勾选的都会搜，前面的优先）").grid(
            row=6, column=0, sticky="nw", pady=6)
        src = ttk.Frame(frm)
        src.grid(row=6, column=1, sticky="w", padx=6, pady=6)
        mu = self.cfg.setdefault("music", {})
        self.srcvars = {}
        for i, (key, label) in enumerate((("kugou", "酷狗"), ("netease", "网易云"),
                                          ("qq", "QQ音乐"))):
            v = tk.BooleanVar(value=(key == "kugou" or
                                     (mu.get(key) or {}).get("enabled", True)))
            self.srcvars[key] = v
            ttk.Checkbutton(src, text=label, variable=v).grid(row=0, column=i,
                                                              sticky="w", padx=(0, 10))
        self.srcstat = tk.StringVar(value="")
        ttk.Label(src, textvariable=self.srcstat, foreground="#888").grid(
            row=1, column=0, columnspan=3, sticky="w")

        self.btn = ttk.Button(frm, text="开始", command=self.toggle)
        self.btn.grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)
        self.status = tk.StringVar(value="未开始")
        ttk.Label(frm, textvariable=self.status, foreground="#555").grid(
            row=8, column=0, columnspan=2, sticky="w")

        self.hint = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.hint, foreground="#888", wraplength=380,
                  justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=6)

        r.protocol("WM_DELETE_WINDOW", self.close)
        r.after(1000, self._tick)
        r.after(400, self._probe_sources)

    # ---------- 控制 ----------
    def toggle(self):
        if self.thread and self.thread.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        live = self.cfg.setdefault("live", {})
        dm = self.cfg.setdefault("danmu", {})
        live["room"] = self.room.get().strip()
        live["sessdata"] = self.sess.get().strip()
        dm["host_title"] = self.host.get().strip()
        dm["side"] = self.side.get()
        dm["screen"] = int(self.screen.get() or 0)
        dm["size"] = int(self.size.get() or 480)
        dm["size_mode"] = self.sizemode.get()
        mu = self.cfg.setdefault("music", {})
        enabled = [k for k in ("kugou", "netease", "qq") if self.srcvars[k].get()]
        if not enabled:
            enabled = ["kugou"]
            self.srcvars["kugou"].set(True)
        mu["order"] = enabled + [k for k in mu.get("order", []) if k not in enabled]
        for k in ("netease", "qq"):
            mu.setdefault(k, {})["enabled"] = k in enabled
        try:
            M.save_cfg(self.cfg, self.cfg_path)
        except Exception as e:
            log.warning("保存配置失败: %s", e)
        if not live["room"]:
            self.hint.set("先填直播间房间号")
            return
        self.stats.clear()
        M.ensure_panel(self.cfg)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.btn.config(text="停止")
        self.status.set("运行中 · 直播间 %s" % live["room"])

    def _run(self):
        try:
            self.danmaku = bili.BiliDanmaku(
                self.cfg["live"]["room"], self.cfg["live"].get("sessdata", ""),
                M.build_handler(self.cfg, self.player, self.push, self.stats))
            self.danmaku.start()
        except Exception as e:
            log.exception("启动失败")
            self.root.after(0, self.hint.set, "启动失败: %s" % e)

    def stop(self):
        try:
            if self.danmaku:
                self.danmaku.stop()
        except Exception:
            pass
        self.danmaku = None
        self.btn.config(text="开始")
        self.status.set("已停止")

    def _tick(self):
        try:
            s = self.stats
            self.hint.set("弹幕 %d 条 · 点歌 %d 次 · 事件 %d" %
                          (s.get("chat", 0), s.get("orders", 0), s.get("events", 0)))
            self.root.after(1000, self._tick)
        except Exception:
            pass

    def _probe_sources(self):
        """后台探一下三家服务在不在，别卡住界面。"""
        def work():
            try:
                import source
                eng = getattr(self, "player", None)
                eng = eng.engine if eng else None
                if eng is None:
                    # 还没点开始，也要能看到酷狗在不在
                    import kgapi
                    eng = kgapi.KugouEngine(self.cfg)
                st = source.Sources(self.cfg, eng).status()
                txt = " · ".join("%s%s" % (x["label"], "✓" if x["up"] else "✗")
                                 for x in st)
            except Exception as e:
                txt = "探测失败：%s" % e
            try:
                self.root.after(0, lambda: self.srcstat.set(txt))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def close(self):
        self.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)   # 面板是独立进程，这里走人不影响它；要一起关就注释这行

    def run(self):
        # 控制窗里的 Player 与主程序共用同一套
        self.push = M.Push(self.cfg.get("danmu", {}).get("port", 9701))
        self.player = M.Player(self.cfg, self.push)
        self.player.start()
        self.root.mainloop()
