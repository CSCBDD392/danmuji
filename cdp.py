"""极简 CDP 客户端：Runtime.evaluate / Page.navigate / Input.dispatchKeyEvent。"""
import itertools
import json
import logging
import time

from websocket import create_connection

log = logging.getLogger("cdp")
_ids = itertools.count(1)


def _http_json(url, method="GET"):
    """CDP 的 HTTP 端点（/json/list 等）。"""
    from urllib.request import Request, urlopen
    with urlopen(Request(url, method=method), timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _open(ws_url, ws_origin=""):
    # 留空 Origin（suppress_origin）才不会被新版 Chromium 以 403 拒绝
    return create_connection(ws_url, timeout=30, origin=ws_origin, suppress_origin=not ws_origin)


def _call(ws_url, method, params, ws_origin=""):
    ws = _open(ws_url, ws_origin)
    i = next(_ids)
    try:
        ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i:
                return m
    finally:
        ws.close()


def call(ws_url, method, params=None, ws_origin=""):
    """任意 CDP 方法（返回原始响应，不抛异常）。"""
    return _call(ws_url, method, params or {}, ws_origin)


def navigate_ctx(ws_url, url, wait=None, ws_origin=""):
    """等页面加载完成再返回；wait=额外再等多久（毫秒）。"""
    ws = _open(ws_url, ws_origin)
    ids = [next(_ids), next(_ids)]
    try:
        ws.send(json.dumps({"id": ids[0], "method": "Page.enable"}))
        ws.send(json.dumps({"id": ids[1], "method": "Page.navigate", "params": {"url": url}}))
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                m = json.loads(ws.recv())
            except Exception:
                break
            if m.get("method") == "Page.loadEventFired":
                break
    finally:
        ws.close()
    if wait:
        time.sleep(wait / 1000.0)


def set_extra_headers(ws_url, headers, ws_origin=""):
    _call(ws_url, "Network.enable", {}, ws_origin)
    _call(ws_url, "Network.setExtraHTTPHeaders", {"headers": headers}, ws_origin)


def user_agent(ws_url, ws_origin=""):
    m = _call(ws_url, "Browser.getVersion", {}, ws_origin)
    try:
        return m["result"]["userAgent"]
    except Exception:
        return ""


def evaluate(ws_url, expr, timeout=30, user_gesture=True, ws_origin=""):
    """在页内执行 JS（awaitPromise），返回 JSON 化后的值。"""
    m = _call(ws_url, "Runtime.evaluate", {
        "expression": expr, "awaitPromise": True, "returnByValue": True,
        "userGesture": user_gesture}, ws_origin)
    res = m.get("result") or {}
    if res.get("exceptionDetails"):
        d = res["exceptionDetails"]
        raise RuntimeError("JS 异常: %s %s" % (d.get("text"),
                                               (d.get("exception") or {}).get("description", "")))
    return (res.get("result") or {}).get("value")


def navigate(ws_url, url, ws_origin=""):
    _call(ws_url, "Page.navigate", {"url": url}, ws_origin)


def key(ws_url, name, code, key_code, ws_origin=""):
    """发一次完整的按键（domKey, code, windowsVirtualKeyCode）。"""
    base = {"key": name, "code": code, "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code}
    _call(ws_url, "Input.dispatchKeyEvent", dict(base, type="keyDown", text="\r" if name == "Enter" else ""), ws_origin)
    _call(ws_url, "Input.dispatchKeyEvent", dict(base, type="keyUp"), ws_origin)
