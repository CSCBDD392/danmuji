"""聚合启动器：点歌机和弹幕机是同居一个屋檐下的两个独立小工具。

  点歌机  Player        —— 收弹幕 → 三家聚合搜歌 → 本地播放（可以完全独立跑）
  弹幕机  DanmuPanel    —— 抓 B 站弹幕 → 正方形面板贴窗口（可以完全独立跑）

两者只通过一个本地 TCP 端口松耦合：点歌机把「▶ 点播《晴天》」这类事件推给
弹幕面板当一行提示；面板没开，点歌机照样正常工作，反之亦然。

单独跑任何一个：

    python main.py --player-only     # 只有点歌机，没有面板
    python danmu.py --test           # 只有弹幕面板（演示数据）

一起跑（聚合）：

    python app.py                    # 全要：点歌 + 弹幕面板
    python app.py --no-player        # 只要面板
    python app.py --no-panel         # 只要点歌（静默播歌，不显示弹幕）
"""
import argparse
import logging
import os
import sys
import time

import kgapi
import main as M
import source

log = logging.getLogger("app")

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cfg, cfg_path, want_player=True, want_panel=True, want_gui=False):
    """聚合运行：按开关决定起哪几个部件。"""
    dm = cfg.get("danmu", {})
    push = M.Push(dm.get("port", 9701)) if (want_player or want_panel) else None

    if want_panel:
        M.ensure_panel(cfg)
        time.sleep(0.8)      # 给面板子进程一点起步时间
        log.info("弹幕机已启动（面板端口 %s）", dm.get("port", 9701))
    if not want_player:
        log.info("只开弹幕机：不点歌")
        if want_panel:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return

    player = M.Player(cfg, push)
    player.start()
    if push:
        push.event("点歌机已上线：发送「点歌 歌名」即可点播")
    log.info("点歌机已启动（音乐源：%s）",
             " / ".join(s.label for s in player.sources.all()))

    live = cfg.get("live", {})
    if not live.get("room") or os.environ.get("DANMUJI_OFFLINE"):
        log.info("离线模式（未配置 room），从 stdin 读弹幕")
        M.offline_feeder(push, player, cfg)
        player.engine.stop()
        return

    import bili
    th = bili.BiliDanmaku(live["room"], live.get("sessdata", ""),
                          M.build_handler(cfg, player, push))
    th.start()
    log.info("直播间 %s 弹幕已连接", live["room"])
    try:
        while th.is_alive():
            th.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        th.stop()
        player.engine.stop()


def sources_report(cfg):
    e = kgapi.KugouEngine(cfg)
    srcs = source.Sources(cfg, e)
    print("音乐源顺序:", " -> ".join(srcs.order))
    for st in srcs.status():
        print("  %-8s %s" % (st["label"], "在线" if st["up"] else "连不上"))
    e.stop()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="点歌机 + 弹幕机 聚合启动器")
    ap.add_argument("--cfg", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--no-player", action="store_true", help="只要弹幕机")
    ap.add_argument("--no-panel", action="store_true", help="只要点歌机（不显示弹幕面板）")
    ap.add_argument("--gui", action="store_true", help="开控制窗（点歌+弹幕都能从这里开关）")
    ap.add_argument("--sources", action="store_true", help="看看三家音乐源通不通")
    a = ap.parse_args()

    cfg = M.load_cfg(a.cfg)
    if a.sources:
        sources_report(cfg)
        return
    if a.gui:
        import gui
        gui.App(cfg, a.cfg).run()
        return
    run(cfg, a.cfg, want_player=not a.no_player, want_panel=not a.no_panel)


if __name__ == "__main__":
    sys.exit(main() or 0)
