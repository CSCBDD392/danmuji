"""三家音乐源的统一适配层：酷狗 / 网易云 / QQ。

三家各有各的脾气，这里只抹平「搜歌」和「拿直链」两件事：

  酷狗   /search?keywords=        /song/url?hash=&album_id=
  网易云 /search?keywords=        /song/url?id=
  QQ     POST /search/byType      /song/urls?mids=

统一后的歌曲字典（字段少的留空，别家不用关心）：
  {source, id, title, artist, album, duration, pay, _raw}
source 取值 "kugou" / "netease" / "qq"，id 是各家自己的活儿：
  酷狗用 FileHash，网易云用数字 id，QQ 用 mid。
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("source")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _http(url, data=None, headers=None, timeout=25, method=None):
    h = {"User-Agent": UA}
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def _json(body):
    try:
        return json.loads(body)
    except Exception:
        return None


class Source(object):
    """一个音乐源。子类实现 search() 和 url()。"""
    name = "?"
    label = "?"

    def __init__(self, host, port, engine=None):
        self.host, self.port = host, port
        self.engine = engine           # 酷狗那边复用已托管的 KugouEngine

    @property
    def base(self):
        return "http://%s:%s" % (self.host, self.port)

    def alive(self):
        """只看服务在不在，不看有没有播放权限（QQ 未登录也能搜）。"""
        if self.engine is not None:
            try:
                self.engine.ensure()      # 酷狗 API 需要时才拉起
            except Exception:
                pass
            return self.engine.alive()
        try:
            urllib.request.urlopen(self.base + "/", timeout=2).close()
            return True
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False

    def search(self, kw, n=8):
        raise NotImplementedError

    def url(self, song):
        """返回 (直链, 说明)。拿不到时直链为 None。"""
        raise NotImplementedError


class KugouSource(Source):
    name = "kugou"
    label = "酷狗"

    def search(self, kw, n=8):
        if self.engine is None:
            return []
        out = []
        for it in self.engine.search(kw, n):
            out.append({
                "source": self.name, "id": it.get("hash"),
                "title": it.get("title"), "artist": it.get("artist"),
                "album": it.get("album"), "duration": it.get("duration") or 0,
                "pay": it.get("pay"), "_raw": it,
            })
        return out

    def url(self, song):
        raw = song.get("_raw") or {}
        return self.engine.song_url(song.get("id"), raw.get("album_id", ""))


class NeteaseSource(Source):
    name = "netease"
    label = "网易云"

    def search(self, kw, n=8):
        q = urllib.parse.urlencode({"keywords": kw, "limit": n})
        code, body = _http(self.base + "/search?" + q)
        d = _json(body) or {}
        songs = ((d.get("result") or {}).get("songs")) or []
        out = []
        for s in songs:
            ar = s.get("ar") or s.get("artists") or []
            al = s.get("al") or s.get("album") or {}
            dur = s.get("dt") or s.get("duration") or 0
            out.append({
                "source": self.name, "id": str(s.get("id")),
                "title": s.get("name") or "",
                "artist": "/".join(a.get("name", "") for a in ar if a.get("name")),
                "album": al.get("name", "") if isinstance(al, dict) else "",
                "duration": int(dur) // 1000, "pay": s.get("fee"), "_raw": s,
            })
        return out

    def url(self, song):
        q = urllib.parse.urlencode({"id": song.get("id")})
        code, body = _http(self.base + "/song/url?" + q, timeout=30)
        d = _json(body)
        if not d:
            return None, (body or "")[:120]
        arr = d.get("data") or []
        if isinstance(arr, dict):
            arr = [arr]
        for it in arr:
            u = it.get("url")
            if u:
                return u, ""
            if it.get("freeTrialInfo"):
                return None, "只有试听片段"
        return None, "无可用地址（可能要 VIP）"


class QQSource(Source):
    name = "qq"
    label = "QQ音乐"

    def search(self, kw, n=8):
        code, body = _http(self.base + "/search/byType",
                           data={"keyword": kw, "num": n, "type": 0})
        d = _json(body) or {}
        bodyd = ((d.get("data") or {}).get("body")) or {}
        songs = bodyd.get("item_song") or []
        if isinstance(songs, dict):
            songs = songs.get("list") or []
        out = []
        for s in songs:
            sg = s.get("singer") or []
            f = s.get("file") or {}
            out.append({
                "source": self.name, "id": s.get("mid"),
                "title": s.get("title") or s.get("name") or "",
                "artist": "/".join(x.get("name", "") for x in sg if x.get("name")),
                "album": ((s.get("album") or {}).get("name") or ""),
                "duration": int(s.get("interval") or 0),
                "pay": None, "_raw": s,
            })
        return out

    def url(self, song):
        q = urllib.parse.urlencode({"mids": song.get("id")})
        code, body = _http(self.base + "/song/urls?" + q, timeout=30)
        d = _json(body) or {}
        it = (d.get("data") or {}).get(song.get("id")) or {}
        u = it.get("url")
        if u:
            return u, ""
        return None, it.get("error") or "无可用地址"


class Sources(object):
    """按配置装配，搜歌时按顺序合并，播放时用命中的源取直链。"""

    def __init__(self, cfg, kugou_engine=None):
        m = cfg.get("music") or {}
        host = m.get("host") or "127.0.0.1"
        self.order = m.get("order") or ["kugou", "netease", "qq"]
        table = {}
        if kugou_engine is not None:
            table["kugou"] = KugouSource(host, kugou_engine.port, kugou_engine)
        for key, cls, dport in (("netease", NeteaseSource, 3000),
                                ("qq", QQSource, 3300)):
            sub = m.get(key) or {}
            if sub.get("enabled", True):
                table[key] = cls(sub.get("host") or host,
                                 int(sub.get("port") or dport))
        self.table = table
        self._down = {}

    def get(self, name):
        return self.table.get(name)

    def label(self, song):
        s = self.table.get(song.get("source"))
        return s.label if s else song.get("source")

    def all(self):
        return [self.table[k] for k in self.order if k in self.table]

    def search(self, kw, n=6):
        """并发搜三家，按 order 排。某家挂了就在这次跳过并记一笔。"""
        import threading
        bag = {}

        def work(s):
            try:
                if not s.alive():
                    self._down[s.name] = True
                    return
                bag[s.name] = s.search(kw, n)
                self._down.pop(s.name, None)
            except Exception as e:
                self._down[s.name] = True
                log.warning("%s 搜索失败: %s", s.name, e)

        ts = [threading.Thread(target=work, args=(s,)) for s in self.all()]
        [t.start() for t in ts]
        [t.join() for t in ts]
        out = []
        for name in self.order:
            out.extend(bag.get(name) or [])
        return out

    def status(self):
        return [{"name": s.name, "label": s.label,
                 "up": not self._down.get(s.name)} for s in self.all()]

    def url(self, song):
        s = self.table.get(song.get("source"))
        if s is None:
            return None, "未知来源"
        try:
            u, why = s.url(song)
        except Exception as e:
            return None, str(e)
        if not u:
            return None, why
        return u, ""

    def url_with_fallback(self, song, tried=None):
        """先按歌本身的来源取；失败再让同名的其它源顶上。

        典型场景：搜索时三家的结果混排，酷狗那条恰好是 VIP 曲子，
        可网易云/QQ 有免费版——没必要让用户等一分钟才知道放不了。
        """
        tried = tried if tried is not None else set()
        names = [song.get("source")] + [n for n in self.order
                                        if n != song.get("source")]
        kw = (song.get("title") or "").strip()
        for i, name in enumerate(names):
            if name in tried:
                continue
            s = self.table.get(name)
            if s is None:
                continue
            if i == 0:
                cand, why = self.url(song)
                if cand:
                    return cand, "", song
                tried.add(name)
                continue
            if not kw:
                break
            try:
                alts = s.search(kw, 4)
            except Exception:
                tried.add(name)
                continue
            for alt in alts:
                if _score(alt, kw) < 0:
                    break
                cand, why = self.url(alt)
                if cand:
                    return cand, "", alt
            tried.add(name)
        return None, why, song

    def best(self, kw, n=5):
        """弹幕点歌用：按标题/歌手匹配度给三家混排结果打分取最优。"""
        cands = self.search(kw, n)
        if not cands:
            return None
        return max(cands, key=lambda c: _score(c, kw))


def _score(song, kw):
    """打分：标题越贴合越高，live/伴奏/翻唱这类降权。

    同一首歌在三个平台都搜得到时，先比分数；酷狗的直链最稳（有 dfid 时
    免费歌基本都能放），所以同分时按 order 里的先后算一点点优势。
    """
    kw = (kw or "").strip().lower()
    t = (song.get("title") or "").lower()
    a = (song.get("artist") or "").lower()
    if not t:
        return -99
    sc = 0.0
    if t == kw:
        sc += 20
    elif kw and kw in t:
        sc += 12
    elif t and t in kw:
        sc += 6
    else:
        sc -= 8
    if kw and kw in a:
        sc += 6
    elif a and any(p and p in kw for p in a.replace("/", " ").split()):
        sc += 4
    for w in ("live", "伴奏", "dj", "翻唱", "remix", "片段", "cover", "纯音乐"):
        if w in t:
            sc -= 5
    order = {"kugou": 1.0, "netease": 0.6, "qq": 0.2}
    sc += order.get(song.get("source"), 0)
    if (song.get("duration") or 0) < 60:
        sc -= 6
    return sc
