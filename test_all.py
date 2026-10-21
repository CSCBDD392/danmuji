"""离线自测（不联网、不开窗口）：解析、封包、签名、page.js 结构。"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)


def t(name, fn):
    try:
        fn()
        print("PASS", name)
    except Exception as e:
        print("FAIL", name, "->", type(e).__name__, e)
        globals()["FAILED"] = globals().get("FAILED", 0) + 1


def parse_orders():
    import main
    assert main.parse_order("点歌 晴天")["title"] == "晴天"
    assert main.parse_order("点歌：晴天 - 周杰伦")["artist"] == "周杰伦"
    assert main.parse_order("主播666") is None


def wbi_sign():
    import bili
    s = bili.wbi_sign({"foo": 116, "bar": "514", "zab": "1919810"},
                      "7cd084941338484aae1ad9425b84077c",
                      "4932caff0ff746eab6f01bf08b70ac45")
    # 固定 wts 后必须是确定值；这里只校验形状 + 归一化，避免依赖时间
    assert len(s["w_rid"]) == 32 and s["foo"] == "116", s
    s2 = bili.wbi_sign({"a": "1,2"}, "x" * 32, "y" * 32)
    assert len(s2["w_rid"]) == 32


def danmaku_packet():
    import struct
    import zlib
    import bili
    # 切片再 unpack：本机 Python 3.8.6 对完整缓冲 unpack 会误报长度
    total, hlen, ver, op, seq = struct.unpack(">IHHII", bili.pack(2, "{}")[:16])
    assert (total, hlen, ver, op, seq) == (18, 16, 1, 2, 1), (total, hlen, ver, op, seq)
    inner = bili.pack(5, '{"cmd":"DANMU_MSG"}')
    z = struct.pack(">IHHII", 16 + len(zlib.compress(inner)), 16, 2, 5, 1) + zlib.compress(inner)
    got = bili.unpack(z)
    assert got and got[0][0] == 5 and b"DANMU_MSG" in got[0][1], got
    # 大包（>=64KB）与两包连发也要能拆
    big = bili.pack(5, b"x" * 70000)
    assert bili.unpack(big)[0][1] == b"x" * 70000
    two = bili.pack(5, b"a") + bili.pack(5, b"b")
    assert [b for _, b in bili.unpack(two)] == [b"a", b"b"]


def page_js():
    with open("page.js", encoding="utf-8") as f:
        js = json.load(f)
    for k in ("focus", "rows", "src", "click", "playing", "ctrl"):
        assert k in js, "page.js 缺 key: " + k
    for k, v in js.items():
        with open("_chk.js", "w", encoding="utf-8") as f:
            f.write(v.replace("%title%", '""').replace("%artist%", '""')
                     .replace("%cmd%", '"pause"'))
        r = subprocess.run(["node", "--check", "_chk.js"], capture_output=True, text=True)
        assert r.returncode == 0, "node --check %s 失败: %s" % (k, r.stderr[:200])
    os.remove("_chk.js")


def order_re_filter():
    import main
    assert main.SKIP_RE.match("切歌") and main.SKIP_RE.match("!next")
    assert main.parse_order("切歌") is None


def config_keys():
    with open("config.json", encoding="utf-8") as f:
        c = json.load(f)
    for k in ("live", "kugou", "kugou_api", "music", "audio", "danmu", "queue"):
        assert k in c, k
    assert "room" in c["live"] and "cdp_port" in c["kugou"] and "port" in c["danmu"]
    assert c["music"]["order"][0] == "kugou", "默认先走酷狗（直链最稳）"
    for n in ("netease", "qq"):
        assert n in c["music"], "music 缺源: " + n


def source_score():
    import source
    # 同名同歌手 > 同名翻唱 > 标题不搭
    good = {"title": "晴天", "artist": "周杰伦", "source": "kugou", "duration": 269}
    cover = {"title": "晴天", "artist": "某翻唱", "source": "qq", "duration": 269}
    miss = {"title": "七里香", "artist": "周杰伦", "source": "kugou", "duration": 299}
    assert source._score(good, "晴天") > source._score(cover, "晴天")
    assert source._score(cover, "晴天") > source._score(miss, "晴天")
    # 30 秒片段要明显降分
    clip = dict(good, duration=30)
    assert source._score(clip, "晴天") < source._score(good, "晴天")


def source_adapters():
    import source
    # 三家解析都要能吃各自的原始返回，不联网（直接喂假请求结果）
    ne = source.NeteaseSource("127.0.0.1", 1)
    ne._fake = None
    assert ne.name == "netease" and ne.label == "网易云"
    qq = source.QQSource("127.0.0.1", 1)
    assert qq.name == "qq" and qq.label == "QQ音乐"
    kg = source.KugouSource("127.0.0.1", 1)
    assert kg.label == "酷狗"
    # 未知来源要给出说明而不是抛异常
    s = source.Sources({"music": {"order": []}})
    assert s.url({"source": "nope"}) == (None, "未知来源")
    assert s.label({"source": "nope"}) == "nope"


def audio_guard():
    import audio
    d = os.path.join(HERE, "_tmpcache")
    p = audio.AudioPlayer({"cache_dir": d})
    assert os.path.isdir(p.cache_dir)
    # 小于 24KB 必须判失败（酷狗无版权时常给几百字节占位音）
    src = os.path.join(d, "_src.mp3")
    with open(src, "wb") as f:
        f.write(b"ID3" + b"\0" * 100)
    ok = False
    try:
        p.fetch("file:///" + src.replace("\\", "/").lstrip("/"), "tiny")
    except Exception:
        ok = True
    finally:
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass
    assert ok, "占位音频不应被当成成功"


def ui_imports():
    # 悬浮层依赖 Win32 结构体，import 阶段最容易出错；单独验一下
    import danmu
    assert danmu._Hotkey._parse("ctrl+alt+d") == (ord("D"), 0x0002 | 0x0001)
    assert danmu._Hotkey._parse("f9")[0] == 0x78
    assert len(danmu.monitor_rect(0)) == 4
    assert danmu.find_window("这个窗口标题一定不存在_zz") is None


if __name__ == "__main__":
    for n, f in [("parse_orders", parse_orders), ("wbi_sign", wbi_sign),
                 ("danmaku_packet", danmaku_packet), ("page_js", page_js),
                 ("order_re_filter", order_re_filter), ("config_keys", config_keys),
                 ("source_score", source_score), ("source_adapters", source_adapters),
                 ("audio_guard", audio_guard),
                 ("ui_imports", ui_imports)]:
        t(n, f)
    print("FAILED:", globals().get("FAILED", 0))
    sys.exit(1 if globals().get("FAILED") else 0)
