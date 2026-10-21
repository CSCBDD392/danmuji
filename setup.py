"""首次运行向导：部署完成后问一句「要不要登录酷狗 VIP」。

按用户要求，提示语固定为：
    目前已部署完毕，但只支持免费歌曲。
    如果你点歌需要付费歌曲的话，请登录你的酷狗VIP账号。
选择「是」进入扫码登录，选择「否」跳过（之后可以在控制台里重新登录）。
"""
import logging
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

log = logging.getLogger("setup")

TITLE = "酷狗音乐引擎"
QUESTION = ("目前已部署完毕，但只支持免费歌曲。\n\n"
            "如果你点歌需要付费歌曲的话，请登录你的酷狗VIP账号。")
LOGIN_LATER = "config.local.json"


def ask_vip(engine, cfg_path=None, gui_parent=None):
    """返回 True=已登录 / False=跳过。阻塞式（自己维护 Tk 主循环）。"""
    root = tk.Toplevel(gui_parent) if gui_parent is not None else tk.Tk()
    if gui_parent is None:
        root.title(TITLE + " - 安装完成")
        root.geometry("460x230")
        root.resizable(False, False)
    else:
        root.title(TITLE)
    root.attributes("-topmost", True)

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=QUESTION, justify="left", wraplength=410).pack(anchor="w")

    status = tk.StringVar(value="")
    ttk.Label(frm, textvariable=status, foreground="#666").pack(anchor="w", pady=(8, 0))

    btns = ttk.Frame(frm)
    btns.pack(anchor="e", pady=12)
    result = {"ok": False, "done": False}

    def finish(ok):
        result["ok"] = ok
        result["done"] = True
        root.destroy()

    def do_login():
        btns.pack_forget()
        status.set("正在向酷狗申请登录二维码…")
        root.update()

        def worker():
            qr = engine.qr_create()
            if not qr or not qr.get("qrcode"):
                root.after(0, lambda: status.set("申请二维码失败，请检查音乐引擎是否在运行"))
                root.after(0, lambda: btns.pack(anchor="e"))
                return
            img_path = qr.get("qrcode") or ""
            ident = None
            for k in ("key", "ident", "qrcode_key", "qrcodekey"):
                if qr.get(k):
                    ident = qr[k]
                    break
            root.after(0, lambda: _show_qr(root, img_path, ident, engine, status, finish))

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(btns, text="是（Y）· 登录酷狗 VIP", command=do_login).pack(side="right", padx=6)
    ttk.Button(btns, text="否（N）· 只用免费歌曲", command=lambda: finish(False)).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    if gui_parent is None:
        root.mainloop()
    else:
        gui_parent.wait_window(root)
    return result["ok"]


def _show_qr(root, img_url, ident, engine, status, finish):
    for w in root.winfo_children():
        w.destroy()
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="请用酷狗 App / 微信扫码登录，登录后会自动继续").pack()
    status.set("等待扫码…")

    photo = None
    try:
        import urllib.request
        data = urllib.request.urlopen(img_url, timeout=15).read() \
            if img_url.startswith("http") else None
        if data:
            p = os.path.join(os.environ.get("TEMP") or ".", "danmuji_qr.png")
            open(p, "wb").write(data)
            photo = tk.PhotoImage(file=p)
            ttk.Label(frm, image=photo).pack(pady=6)
            root._qr_photo = photo
    except Exception as e:
        log.info("二维码图片加载失败: %s", e)
        ttk.Label(frm, text="（二维码图片加载失败，请改用浏览器打开酷狗官网扫码）").pack()

    def poll():
        if not root.winfo_exists():
            return
        try:
            r = engine.qr_check(ident)
            code = (r or {}).get("status")
            if code in (2, 4) or str(code) == "2":     # 2/4 表示扫码成功
                status.set("登录成功！正在保存…")
                _save_cookie(engine, r)
                root.update()
                time.sleep(0.4)
                finish(True)
                return
        except Exception:
            pass
        root.after(2000, poll)

    ttk.Button(frm, text="跳过", command=lambda: finish(False)).pack(pady=8)
    if ident:
        root.after(2000, poll)


def _save_cookie(engine, resp):
    """把登录返回的 cookie 落到 config.local.json，下次启动自动带上。"""
    try:
        import json
        d = (resp or {}).get("data") or {}
        tok = d.get("token") or d.get("t1") or ""
        uid = d.get("userid") or d.get("uid") or ""
        if tok:
            engine.token = str(tok)
            engine.userid = str(uid)
        log.info("酷狗登录态已保存")
    except Exception as e:
        log.warning("保存登录态失败: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from kgapi import KugouEngine
    import json
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "config.json"),
                        encoding="utf-8"))
    e = KugouEngine(cfg)
    print("engine ready:", e.ensure())
    print("vip login:", ask_vip(e))
