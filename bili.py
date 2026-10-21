"""B 站直播间弹幕客户端（只用 requests + websocket-client）。"""
import json
import logging
import re
import struct
import threading
import time
from hashlib import md5

import requests
import websocket  # websocket-client >= 1.2

log = logging.getLogger("bili")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}


def room_id(x):
    """支持直播间 URL / 房间号。"""
    m = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", str(x))
    return int(m.group(1)) if m else int(x)


def wbi_sign(params, img_key, sub_key):
    mixin = "".join(img_key[i] + sub_key[i] for i in range(32))
    params = dict(params)
    params["wts"] = int(time.time())
    params = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["w_rid"] = md5((q + mixin).encode()).hexdigest()
    return params


def _safe_send(ws, data):
    try:
        ws.send(data, True)
    except Exception:
        pass


def pack(op, body=b""):
    """弹幕协议包头：packet_len(4) header_len(2)=16 ver(2) op(4) seq(4)。"""
    body = body if isinstance(body, bytes) else body.encode()
    return struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1) + body


def unpack(raw):
    """拆包，返回 [(op, body)]；ver=2 zlib / ver=3 brotli 会递归展开。

    注意：位置参数切片（raw[i:i+12]）——本机 Python 3.8.6 下对完整缓冲做
    struct.unpack 会抛 "requires a buffer of N bytes"，切片可绕开。
    """
    out, i = [], 0
    while i + 16 <= len(raw):
        plen, hlen, ver, op = struct.unpack(">IHHI", raw[i:i + 12])
        body = raw[i + hlen:i + plen]
        i += plen
        if ver == 2:
            import zlib
            try:
                out += unpack(zlib.decompress(body))
            except Exception:
                pass
        elif ver == 3:
            try:
                import brotli
                out += unpack(brotli.decompress(body))
            except ImportError:
                log.debug("收到 brotli 包但未安装 brotli，已跳过")
        else:
            out.append((op, body))
    return out


class Bili:
    def __init__(self, sessdata=""):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        if sessdata:
            self.s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")

    def _get(self, url, **kw):
        r = self.s.get(url, timeout=10, **kw)
        r.raise_for_status()
        j = r.json()
        if j.get("code", 0) != 0:
            raise RuntimeError(f"bili api {url}: {j.get('code')} {j.get('message')}")
        return j["data"]

    def _wbi_keys(self):
        nav = self._get("https://api.bilibili.com/x/web-interface/nav")
        wbi = nav["wbi_img"]
        return wbi["img_url"].rsplit("/", 1)[1].split(".")[0], \
            wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]

    def resolve(self, rid):
        """真实房间号、主播 UID、标题。"""
        d = self._get("https://api.live.bilibili.com/room/v1/Room/get_info",
                      params={"room_id": rid})
        return d["room_id"], d["uid"], d["title"]

    def danmaku_token(self, rid):
        """弹幕服务器地址 + token。

        带 SESSDATA 时先用 wbi 签名（部分房间动态加密），失败自动降级为匿名；
        匿名也能收到普通弹幕。
        """
        if self.s.cookies.get("SESSDATA"):
            try:
                ik, sk = self._wbi_keys()
                p = wbi_sign({"id": rid, "type": 0, "web_location": "444.8"}, ik, sk)
                d = self._get("https://api.live.bilibili.com/xlive/web-room/v2/"
                              "index/getDanmuInfo", params=p)
                return d["host_list"][0], d["token"]
            except Exception as e:  # 降级为匿名
                log.info("wbi 取 token 失败，改用匿名: %s", e)
        d = self._get("https://api.live.bilibili.com/xlive/web-room/v1/"
                      "index/getDanmuInfo", params={"id": rid})
        return d["host_list"][0], d["token"]


GUARD = {1: "总督", 2: "提督", 3: "舰长"}


def _norm_color(v, default="ffffff"):
    v = str(v or "").strip().lstrip("#")
    return v if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v) else default


def user_of(info):
    """从 DANMU_MSG.info 里解析发送者信息。

    info[0] 新版是 [uid, uname, 是否房管, 是否月费 0|1|2, 粉丝牌?, 等级, ..., 头像?...]，
    老版是 [发送时间, 等级, 昵称, uid, ...]；用户名颜色/头衔在 info[3]。
    返回 {'uid','user','color','medal','medal_level','medal_color','guard',
          'is_admin','is_vip','level','face','time'}
    """
    head = info[0] if info and isinstance(info[0], list) else []

    def at(i, d=None):
        return head[i] if len(head) > i else d

    meta = info[3] if len(info) > 3 and isinstance(info[3], list) else []

    def m(i, d=None):
        return meta[i] if len(meta) > i else d

    # 昵称 / uid / 等级 / 房管 / 月费：新版老版分别取
    if head and isinstance(at(1), str) and at(1):
        user, uid = at(1), at(0)
        admin, vip = bool(at(2)), at(3) in (1, 2)
        level = at(6) if isinstance(at(6), int) else (at(1) if isinstance(at(1), int) else 0)
        # 新版头像是 uinfo 对象里的 face
        face = ""
        uinfo = m(0)
        if isinstance(uinfo, dict):
            face = uinfo.get("face") or ""
    else:
        user, uid = at(2) or "", at(3)
        admin, vip, level = bool(at(2)), False, at(1) if isinstance(at(1), int) else 0
        face = ""
    if not face:  # 老格式：info[0][15] / 外面再兜底
        uinfo = at(15) or m(0)
        if isinstance(uinfo, dict) and uinfo.get("user"):
            user = user or uinfo["user"].get("uname", "")
            uid = uid or uinfo["user"].get("uid")
            face = uinfo["user"].get("face") or face

    medal = m(1) or ""
    if isinstance(medal, dict):        # 个别版本这里是对象
        medal = medal.get("medal_name", "")
    medal = str(medal or "").strip()
    guard = m(7) if isinstance(m(7), int) else 0
    if not guard and isinstance(at(7), int):   # 旧包里 guard_level 在 info[0][7]
        guard = at(7)

    return {
        "uid": uid, "user": str(user or ""),
        "color": _norm_color(m(2)), "face": face,
        "medal": medal,
        "medal_level": m(3) if isinstance(m(3), int) else 0,
        "guard": guard, "is_admin": admin, "is_vip": vip,
        "level": int(level or 0),
        "time": time.strftime("%H:%M:%S"),
    }


def event_of(cmd, d):
    """礼物/上舰/进场等事件 → 一行文字；不关心的事件返回 None。"""
    if cmd == "SEND_GIFT":
        j = d.get("data") or {}
        n = j.get("num", 1)
        name = j.get("giftName", "礼物")
        who = (j.get("uname") or "")
        act = "投喂" if not j.get("combo_send") else "送出"
        return "🎁 %s %s %s×%d" % (who, act, name, n), j.get("uid"), who
    if cmd.startswith("USER_TOAST_MSG"):
        j = d.get("data") or {}
        who = (j.get("uname") or "").replace(" ", "")
        role = j.get("role_name") or "大航海"
        n = j.get("unit", 1)
        return "👑 %s %s了 %s×%s" % (who, "开通", role, n), j.get("uid"), who
    if cmd == "GUARD_BUY":
        j = d.get("data") or {}
        return "👑 %s 开通 %s ×%s" % (j.get("username", ""), j.get("gift_name", "大航海"),
                                      j.get("num", 1)), j.get("uid"), j.get("username", "")
    if cmd == "INTERACT_WORD":
        j = d.get("data") or {}
        if j.get("msg_type") == 2:      # 只是关注，忽略
            return None
        who = j.get("uname") or ""
        return "👋 %s 进入直播间" % who if who else None, j.get("uid"), who
    if cmd == "WATCHED_CHANGE":
        j = d.get("data") or {}
        return "📈 看过人数 %s" % j.get("num", ""), None, ""
    if cmd == "SUPER_CHAT_MESSAGE":
        j = d.get("data") or {}
        u = j.get("user_info") or {}
        return "💬 %s 的醒目留言：%s" % (u.get("uname", ""), j.get("message", "")), \
            u.get("uid"), u.get("uname", "")
    return None


class BiliDanmaku(threading.Thread):
    """连接直播间弹幕 WS，回调 cb(kind, payload)，kind ∈ chat/other。自动重连。"""

    def __init__(self, room, sessdata="", cb=None):
        super().__init__(daemon=True, name=f"danmaku-{room}")
        self.rid = room_id(room)
        self.sessdata = sessdata
        self.cb = cb or (lambda *a: None)
        self._stop = threading.Event()
        self._ws = None
        self.uid = None
        self.title = ""

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ---- protocol ----
    _pack = staticmethod(pack)

    def _on_msg(self, ws, raw, *a):
        if isinstance(raw, str):
            return
        for op, body in unpack(raw):
            if op == 3:  # popularity
                continue
            if op != 5:
                continue
            try:
                d = json.loads(body)
            except Exception:
                continue
            cmd = d.get("cmd", "")
            if cmd.startswith("DANMU_MSG"):
                info = d.get("info") or []
                u = user_of(info)
                u["text"] = info[1] if len(info) > 1 else ""
                u["kind"] = "chat"
                self.cb("chat", u)
                continue
            if cmd in ("LIVE", "LIVE_END", "ROOM_CLOSE"):
                self.cb("other", {"cmd": cmd})
                continue
            try:
                ev = event_of(cmd, d)
            except Exception:
                ev = None
            if ev:
                text, uid, uname = ev
                self.cb("event", {"kind": "event", "text": text, "uid": uid, "user": uname,
                                  "color": "ffffff", "medal": "", "guard": 0,
                                  "is_admin": False, "is_vip": False, "level": 0,
                                  "face": "", "time": time.strftime("%H:%M:%S")})

    def _auth(self, ws, roomid, token):
        # protover=2 即要求 zlib 压缩；订阅后 5 秒内补一个心跳
        ws.send(self._pack(7, json.dumps({
            "uid": 0, "roomid": roomid, "protover": 2, "platform": "web",
            "type": 2, "key": token})), True)
        threading.Timer(4, lambda: _safe_send(ws, self._pack(2, "{}"))).start()

    def run(self):
        api = Bili(self.sessdata)
        while not self._stop.is_set():
            try:
                self.rid, self.uid, self.title = api.resolve(self.rid)
                real = self.rid
                host, token = api.danmaku_token(real)
                url = "wss://%s:%s/sub" % (host["host"], host["wss_port"])
                self._ws = ws = websocket.WebSocketApp(
                    url, on_message=self._on_msg, on_error=lambda *a: None,
                    header=[f"{k}: {v}" for k, v in HEADERS.items()])
                ws.on_open = lambda w, r=real, t=token: self._auth(w, r, t)
                log.info("已连接直播间 %s（%s）", real, self.title)
                ws.run_forever(ping_interval=0)
            except Exception as e:
                log.warning("弹幕连接异常: %s", e)
            if self._stop.wait(5):
                break
