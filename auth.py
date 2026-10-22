# -*- coding: utf-8 -*-
"""访问口令：WebUI 暴露到公网后，光靠链接谁都能控制你的机器，必须加一道门。

规则（故意做得简单，好实现也好排查）：
  * 口令存在 config.local.json 的 webui.token 里，第一次启动自动生成。
  * 本机（127.0.0.1 / ::1）访问不用口令，本地用着最顺手。
  * 从任何别的地址来（CF 隧道、局域网），必须带口令：
      - 浏览器：http://.../?t=<口令>  会自动种 cookie，之后就不用带了
      - 脚本：  Header  X-Danmu-Token: <口令>
              或 Query ?t=<口令>
  * 口令比对用 hmac.compare_digest，不做长度侧信道。
"""
import hashlib
import hmac
import os
import secrets

COOKIE = "danmu_tok"


def gen_token():
    """32 位 URL 安全随机串，够用且能直接贴进地址栏。"""
    return secrets.token_urlsafe(24)


def cookie_value(token):
    """cookie 里存的是口令的哈希，不存明文 —— 防止被别的脚本直接读走。"""
    return hmac.new(token.encode(), b"danmuji-cookie", hashlib.sha256).hexdigest()[:32]


def check(headers, query, client_ip, token, cookie_hdr=""):
    """返回 (ok, reason, set_cookie)。

    set_cookie 非空时调用方应该往响应里加 Set-Cookie。
    """
    ip = (client_ip or "").strip()
    local = ip in ("127.0.0.1", "::1", "localhost")

    # 本机直连一律放行：在服务器上自己用不该还要输口令
    if local:
        return True, "local", ""

    if not token:
        # 没设口令：只允许本机，绝不对外开放
        return False, "没设访问口令，禁止外网访问。在 config.local.json 里设置 webui.token", ""

    supplied = ""
    if query.get("t"):
        supplied = query.get("t")[0]
    elif headers.get("X-Danmu-Token"):
        supplied = headers.get("X-Danmu-Token")
    elif headers.get("Authorization", "").lower().startswith("bearer "):
        supplied = headers.get("Authorization")[7:].strip()
    elif cookie_hdr:
        # cookie 形如 danmu_tok=xxxx
        for part in cookie_hdr.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                supplied = v
                break
        # cookie 里是哈希，比值而不是比原文
        if supplied:
            if hmac.compare_digest(supplied, cookie_value(token)):
                return True, "cookie", ""
            return False, "访问口令不对", ""

    if not supplied:
        return False, "需要访问口令（在地址后面加 ?t=口令）", ""
    if hmac.compare_digest(supplied, token):
        # 通过 query 进来的，顺手种 cookie，刷新/翻页就不用再带
        return True, "token", "%s=%s; Path=/; Max-Age=2592000; SameSite=Lax" % (
            COOKIE, cookie_value(token))
    return False, "访问口令不对", ""


def ensure_token(cfg):
    """没有口令就生成一个，写回 config.local.json。返回口令。"""
    web = cfg.setdefault("webui", {})
    tok = (web.get("token") or "").strip()
    if tok:
        return tok
    tok = gen_token()
    web["token"] = tok
    return tok
