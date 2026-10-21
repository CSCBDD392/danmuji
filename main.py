"""点歌机主程序：B 站弹幕 → 点歌解析 → 酷狗播放，同时把弹幕推给弹幕面板。

播放后端优先用内置的酷狗 API 直链（本地播放，不依赖浏览器），取不到再退回
酷狗网页版。

  python main.py                       # 正常模式（读 config.json 的 live.room）
  python main.py --once 晴天            # 只测一条点歌（不连 B 站）
  python main.py --offline             # 不连 B 站，用本地 stdin 模拟弹幕（调试用）
  python main.py --selftest            # 离线自测解析
  python main.py --panel               # 只开弹幕面板（点歌另开）
  python main.py --gui                 # 开个小控制窗口（推荐日常用）
  python main.py --setup               # 重新走一遍首次运行向导（酷狗 VIP 登录）
"""
import argparse
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

import bili
import cdp
import kugou
import kgapi
from audio import AudioPlayer
from danmu import emit

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("main")

# 「点歌 晴天」「点歌：晴天 - 周杰伦」「【点歌】晴天」「来一首 晴天」「!点歌晴天」
ORDER_RE = re.compile(
    r"^[!！]?[【\[(（]?\s*(?:点歌|点播|来一首|来首|放一首|放首|唱一首|唱首|听一首|听首)"
    r"\s*[】\])）]?\s*[:：,，]?\s*(.+)$", re.I)

SKIP_RE = re.compile(r"^[!！]?\s*(切歌|下一首|跳过|next|skip)$", re.I)

with open(os.path.join(HERE, "page.js"), encoding="utf-8") as _f:
    PAGE_JS = json.load(_f)


def parse_order(text):
    """→ {'title', 'artist', 'raw'}；不是点歌指令返回 None。"""
    t = (text or "").strip()
    m = ORDER_RE.match(t)
    if not m:
        return None
    body = m.group(1).strip().strip("《》<>\"'“”「」〖〗").strip()
    if not body:
        return None
    title, artist = body, ""
    for sep in (" - ", "-", "—", "–", "|", "/", "／"):
        if sep in body:
            a, b = body.split(sep, 1)
            if b.strip():
                title, artist = a.strip(), b.strip()
                break
    title = title.strip().strip("《》<>\"'“”「」〖〗")
    if not title or len(title) > 60:
        return None
    return {"title": title, "artist": artist, "raw": t}


def save_cfg(cfg, path=None):
    """写回本机覆盖文件，不动随包分发的 config.json。"""
    p = path or os.path.join(HERE, "config.local.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_cfg(path=None):
    """默认配置 + 本机覆盖（config.local.json），后者优先。"""
    p = path or os.path.join(HERE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise SystemExit("缺少配置文件: %s" % p)
    except Exception as e:
        raise SystemExit("配置文件解析失败 %s: %s" % (p, e))
    local = os.path.join(HERE, "config.local.json")
    if os.path.isfile(local):
        try:
            with open(local, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
        except Exception as e:
            log.warning("config.local.json 读取失败：%s", e)
    return cfg


class Push:
    """往弹幕面板推一条消息（面板没开就算了）。"""

    def __init__(self, port):
        self.port = port
        self.enabled = bool(port) and int(port) > 0
        self.ok = False

    def __call__(self, text, m=None):
        """text: 纯文本兜底；m: 弹幕字典（m['text'] 为空时用 text）。"""
        if not self.enabled:
            return
        msg = dict(m or {})
        msg.setdefault("text", text)
        try:
            emit(self.port, msg.pop("text"), **msg)
            self.ok = True
        except Exception:
            if self.ok:  # 只提示一次
                log.warning("弹幕面板连接断开（端口 %s）", self.port)
            self.ok = False

    def event(self, text):
        self(text, {"kind": "event", "color": "ffd166",
                    "time": time.strftime("%H:%M:%S")})


class Player(threading.Thread):
    """串行播放队列：优先用内置酷狗 API 直链播放，取不到再退回网页版。"""

    def __init__(self, cfg, push):
        super().__init__(daemon=True, name="player")
        self.q = queue.Queue()
        self.cfg = cfg
        self.push = push
        self.searcher = kugou.KugouSearch()
        self.page = None              # 惰性创建：只有退回网页版时才拉起浏览器
        self.engine = kgapi.KugouEngine(cfg)
        self.audio = AudioPlayer(cfg.get("audio"))
        kc = cfg.get("kugou", {})
        qc = cfg.get("queue", {})
        self.gap = float(qc.get("gap_sec", kc.get("gap_sec", 2)))
        self.wait_song = bool(qc.get("wait_song_end", True))
        self.max_pending = int(qc.get("max_pending", 5))
        self.current = None
        self._lock = threading.Lock()   # current 在播放线程与主线程（切歌）之间共享

    # ---------------- 播放后端 ----------------
    def _page(self):
        if self.page is None:
            self.page = kugou.Page(self.cfg.get("kugou"))
        return self.page

    def _play_api(self, song):
        """用内置 API 播放：搜索 → 取直链 → 下载 → 本地播放。"""
        try:
            if not self.engine.ensure():
                return None
        except Exception as e:
            log.info("酷狗 API 不可用：%s", e)
            return None
        cands = self.engine.search(song["title"], 10)
        if not cands:
            return None
        want_a = (song.get("artist") or "").lower()

        def score(c):
            s = 0
            if c["title"] == song["title"]:
                s += 20
            elif song["title"] in c["title"]:
                s += 12
            else:
                s -= 8
            got_a = (c.get("artist") or "").lower()
            if want_a and want_a in got_a:
                s += 12
            if any(x in c["title"] for x in ("live", "伴奏", "dj", "翻唱", "remix", "片段")):
                s -= 5
            if any(x in got_a for x in ("儿歌", "童声", "翻唱", "cover")):
                s -= 6
            if c.get("filesize"):
                # 完整版一般 3~12MB，明显偏小的多半是试听/片段
                if c["filesize"] < 900 * 1024:
                    s -= 8
            return s

        cands.sort(key=score, reverse=True)
        best_trial = None
        for c in cands[:5]:
            url, why = self.engine.song_url(c["hash"], c.get("album_id"))
            if not url:
                log.info("取址失败 %s：%s", c["title"], why)
                continue
            trial = "/yp/p_" in url and "/yp/full/" not in url
            if trial:
                # 免费分片：先记着，等确认没有完整版再退而求其次
                if best_trial is None:
                    best_trial = (c, url)
                log.info("%s 只有试听片段，继续找完整版", c["title"])
                continue
            try:
                path = self.audio.fetch(url, "%s - %s" % (c.get("artist") or "", c["title"]))
            except Exception as e:
                log.warning("下载失败：%s", e)
                continue
            return {"ok": True, "how": "api", "song": c, "path": path, "trial": False}
        if best_trial:
            c, url = best_trial
            try:
                path = self.audio.fetch(url, "%s - %s" % (c.get("artist") or "", c["title"]))
                self.push.event("这首歌只有试听片段，登录酷狗 VIP 可听完整版")
                return {"ok": True, "how": "api", "song": c, "path": path, "trial": True}
            except Exception as e:
                log.warning("试听片段下载失败：%s", e)
        return None

    def order(self, song, user=""):
        if self.q.qsize() >= self.max_pending:
            self.push.event("队列满了，等会儿再点吧")
            return False
        self.q.put((song, user))
        return True

    def skip(self):
        """切歌：停掉本地声音，并关掉可能存在的网页版页签。"""
        with self._lock:
            cur, self.current = self.current, None
        try:
            self.audio.stop()
        except Exception:
            pass
        if cur and cur.get("tab"):
            try:
                self._page().br.close(cur["tab"])
            except Exception:
                pass

    def run(self):
        while True:
            song, user = self.q.get()
            try:
                res = self._play_api(song)
                if res is None:
                    self.push.event("《%s》没取到酷狗直链，改用网页版" % song["title"])
                    res = self._play_web(song)
                if res is None:
                    self.push.event("《%s》暂时放不了" % song["title"])
                    continue
                with self._lock:
                    self.current = res
                if res.get("how") == "api":
                    hit = res["song"]
                    self.push.event("▶ 点播《%s》- %s" % (hit["title"], hit.get("artist") or ""))
                    log.info("本地播放：%s", res["path"])
                    self.audio.play(res["path"], block=True)      # 阻塞到播完
                elif res.get("ok"):
                    if self.wait_song:
                        self._wait_end(res.get("tab"))
            except Exception as e:
                log.exception("播放失败")
                self.push.event("播放失败：%s（%s）" % (song["title"], type(e).__name__))
            with self._lock:
                self.current = None
            time.sleep(self.gap)

    def _play_web(self, song):
        """退回网页版：用 Edge/Chrome 打开酷狗混音页。"""
        try:
            page = self._page()
            hit = self.searcher.best(song["title"], song.get("artist", ""))
            if not hit:
                self.push.event("没搜到《%s》，换首歌吧" % song["title"])
                return None
            self.push.event("▶ 点播《%s》- %s" % (hit["title"], hit["artist"]))
            res = page.play(hit)
            res["how"] = "web"
            log.info("网页版播放结果：%s", {k: v for k, v in res.items() if k != "tab"})
            if not res.get("ok"):
                self.push.event("《%s》暂时放不了（可能要在酷狗网页里点一下播放/登录）"
                                % hit["title"])
            return res
        except Exception as e:
            log.warning("网页版播放失败：%s", e)
            return None

    @staticmethod
    def _state(ws):
        try:
            return cdp.evaluate(ws, PAGE_JS["src"], timeout=5)
        except Exception:
            return None

    def _wait_end(self, tab, poll=3.0, limit=900):
        """等歌放完：页面里的 <audio> 走完就关页签。"""
        if not tab:
            return
        ws = tab.get("webSocketDebuggerUrl")
        end = time.time() + limit
        stalled = 0
        while time.time() < end:
            time.sleep(poll)
            if not any(t.get("id") == tab.get("id") for t in self.page.br._tabs()):
                return  # 页签已经没了（被切歌或用户关掉）
            st = self._state(ws)
            if not st or not st.get("url"):
                stalled += 1
                if stalled >= 3:
                    return
                continue
            stalled = 0
            if not st.get("playing"):
                break
        try:
            self.page.br.close(tab)
        except Exception:
            pass


def panel_running(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def ensure_panel(cfg):
    """弹幕面板没在运行就从本程序拉起来（独立进程）。"""
    dm = cfg.get("danmu", {})
    port = dm.get("port", 9701)
    if panel_running(port):
        return None
    args = [sys.executable, os.path.join(HERE, "danmu.py"), "--port", str(port)]
    if dm.get("host_title"):
        args += ["--host", dm["host_title"]]
    if dm.get("app"):
        args += ["--app", dm["app"]]
    if dm.get("side"):
        args += ["--side", dm["side"]]
    if dm.get("size"):
        args += ["--size", str(dm["size"])]
    if dm.get("size_mode"):
        args += ["--size-mode", dm["size_mode"]]
    if dm.get("screen"):
        args += ["--screen", str(dm["screen"])]
    flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    log.info("拉起弹幕面板: %s", " ".join(args))
    return subprocess.Popen(args, creationflags=flags, close_fds=True)


# 兼容旧名字
ensure_layer = ensure_panel
layer_running = panel_running


def offline_feeder(push, player, cfg):
    """从 stdin 读行当弹幕：'昵称: 文本' 或纯文本。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        user, _, text = line.partition(": ") if ": " in line else ("观众", "", line)
        text = text or line
        push("%s：%s" % (user, text), {"kind": "chat", "user": user, "text": text,
                                      "time": time.strftime("%H:%M:%S")})
        if SKIP_RE.match(text):
            player.skip()
            continue
        song = parse_order(text)
        if song:
            player.order(song, user)


def build_handler(cfg, player, push, stats=None):
    """弹幕 → 面板/点歌 的分发器。stats 传字典进来可以看计数（GUI 用）。"""
    live = cfg.get("live", {})
    seen = set()
    cool = {}
    cooldown = float(live.get("user_cooldown_sec", 20))
    max_len = int(live.get("max_order_len", 40))
    cmds = {c.lower() for c in live.get("admin", []) or []}
    stats = stats if stats is not None else {}

    def on_msg(kind, d):
        if kind == "event":                     # 礼物/上舰/进场
            push("", d)
            stats["events"] = stats.get("events", 0) + 1
            return
        if kind != "chat":
            log.debug("直播事件: %s", d.get("cmd"))
            return
        user, uid = d.get("user"), d.get("uid")
        text = (d.get("text") or "").strip()
        key = (uid, text)
        if key in seen:
            return
        seen.add(key)
        if len(seen) > 3000:
            seen.clear()
        push(user, d)
        stats["chat"] = stats.get("chat", 0) + 1
        if not live.get("enabled", True):
            return
        if SKIP_RE.match(text):
            player.skip()
            return
        song = parse_order(text)
        if not song:
            return
        if len(song["title"]) > max_len:
            push.event("@%s 歌名太长了（最多 %d 字）" % (user, max_len))
            return
        now = time.time()
        # 管理员（主播/房管昵称或 UID）不受冷却限制
        admin = uid in cmds or (user or "").lower() in cmds
        if uid and not admin and now - cool.get(uid, 0) < cooldown:
            log.info("冷却中，忽略 %s: %s", user, text)
            return
        cool[uid] = now
        log.info("点歌 %s -> %s", user, song)
        stats["orders"] = stats.get("orders", 0) + 1
        player.order(song, user)

    return on_msg


def first_run(cfg, cfg_path):
    """首次运行时，先拉起酷狗引擎，再问一句要不要登录 VIP。

    用户要求：部署完成后弹出
        目前已部署完毕，但只支持免费歌曲。
        如果你点歌需要付费歌曲的话，请登录你的酷狗VIP账号。
    并让用户选 是(Y) / 否(N)。只在第一次问，之后写 config.local.json 记住。
    """
    kc = cfg.get("kugou_api", {})
    if kc.get("setup_done"):
        return
    engine = None
    try:
        import setup as setup_mod
        engine = kgapi.KugouEngine(cfg)
        engine.ensure()
        ok = setup_mod.ask_vip(engine, cfg_path)
        log.info("首次向导：%s", "已登录" if ok else "跳过（仅免费歌曲）")
    except Exception as e:
        log.warning("首次向导失败（不影响使用）：%s", e)
    kc["setup_done"] = True
    if engine is not None:
        if engine.dfid:
            kc["dfid"] = engine.dfid
        if engine.token:
            kc["token"] = engine.token
        if engine.userid:
            kc["userid"] = engine.userid
    cfg["kugou_api"] = kc
    try:
        save_cfg(cfg, cfg_path)
    except Exception as e:
        log.warning("写回配置失败：%s", e)


def run(cfg, panel=True):
    dm = cfg.get("danmu", {})
    push = Push(dm.get("port", 9701))
    if panel:
        ensure_panel(cfg)
        time.sleep(0.8)  # 给面板一点启动时间
    player = Player(cfg, push)
    player.start()
    push.event("点歌机已上线：发送「点歌 歌名」即可点播")

    live = cfg.get("live", {})
    if not live.get("room") or os.environ.get("DANMUJI_OFFLINE"):
        log.info("离线模式（未配置 room），从 stdin 读弹幕")
        offline_feeder(push, player, cfg)
        return

    th = bili.BiliDanmaku(live["room"], live.get("sessdata", ""),
                          build_handler(cfg, player, push))
    th.start()
    log.info("点歌机已启动，直播间 %s", live["room"])
    try:
        while th.is_alive():
            th.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        th.stop()
        player.engine.stop()


def selftest():
    cases = {
        "点歌 晴天": ("晴天", ""),
        "点歌：晴天 - 周杰伦": ("晴天", "周杰伦"),
        "【点歌】起风了 - 买辣椒也用券": ("起风了", "买辣椒也用券"),
        "来一首《稻香》": ("稻香", ""),
        "主播666": None,
        "play something": None,
        "点歌": None,
    }
    for text, want in cases.items():
        got = parse_order(text)
        got = (got["title"], got["artist"]) if got else None
        assert got == want, (text, got, want)
    assert SKIP_RE.match("切歌") and not parse_order("切歌")
    print("selftest OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", default="", help="只测一条点歌")
    ap.add_argument("--offline", action="store_true", help="不连 B 站，stdin 当弹幕源")
    ap.add_argument("--panel", "--layer", dest="panel", action="store_true",
                    help="只开弹幕面板")
    ap.add_argument("--gui", action="store_true", help="开控制窗口")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--setup", action="store_true",
                    help="重新走首次运行向导（问要不要登录酷狗 VIP）")
    ap.add_argument("--cfg", default=os.path.join(HERE, "config.json"))
    a = ap.parse_args()
    if a.offline:
        os.environ["DANMUJI_OFFLINE"] = "1"
    if a.selftest:
        selftest()
        raise SystemExit(0)
    cfg = load_cfg(a.cfg)
    if a.gui:
        import gui
        gui.App(cfg, a.cfg).run()
    elif a.panel:
        ensure_panel(cfg)
        time.sleep(1.0)
        print("弹幕面板端口 %s 已就绪" % cfg.get("danmu", {}).get("port", 9701))
        raise SystemExit(0)
    elif a.once:
        s = parse_order("点歌 " + a.once) or {"title": a.once, "artist": ""}
        p = Player(cfg, Push(0))
        print("酷狗引擎就绪:", p.engine.ensure())
        res = p._play_api(s)
        if res:
            print("直链播放:", res["song"]["title"], "-", res["song"].get("artist"))
            print("文件:", res["path"])
            p.audio.play(res["path"], block=True)
        else:
            print("直链取不到，退回网页版：")
            print(p._play_web(s))
    elif a.setup:
        import setup as setup_mod
        e = kgapi.KugouEngine(cfg)
        print("酷狗引擎就绪:", e.ensure())
        ok = setup_mod.ask_vip(e, a.cfg)
        print("登录结果:", "已登录 VIP" if ok else "跳过，仅免费歌曲")
        save_cfg(cfg, a.cfg)
    else:
        first_run(cfg, a.cfg)
        run(cfg)
