"""把内置运行时（node.exe 等大文件）发到 GitHub Release，再回传下载地址。

为什么不走仓库：GitHub 的 Git Data API 对单个 blob 有实际体积上限，
node.exe 89MB 会被拒（`Sorry, your input was too large to process`）。
Release 附件能到 2GB，所以大文件放 Release，仓库里只放代码。

用法：
    python publish_release.py --user CSCBDD392 --repo danmuji --tag runtime-v1 \
        --file vendor/kugou-api/node.exe
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def token_of(explicit=""):
    for v in (explicit, os.environ.get("GITHUB_TOKEN"), os.environ.get("GH_TOKEN")):
        if v:
            return v.strip()
    p = os.path.join(HERE, ".github-token")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def req(method, path, token, data=None, timeout=300):
    url = path if path.startswith("http") else API + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method)
    r.add_header("Authorization", "token " + token)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "danmuji-publisher")
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def upload_asset(user, repo, release_id, path, token):
    """流式上传大文件（不整体读进内存）。"""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    url = "%s/repos/%s/%s/releases/%s/assets?name=%s" % (UPLOAD, user, repo, release_id, name)
    with open(path, "rb") as f:
        r = urllib.request.Request(url, data=f, method="POST")
        r.add_header("Authorization", "token " + token)
        r.add_header("Content-Type", "application/octet-stream")
        r.add_header("Content-Length", str(size))
        r.add_header("User-Agent", "danmuji-publisher")
        try:
            with urllib.request.urlopen(r, timeout=1800) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--repo", default="danmuji")
    ap.add_argument("--tag", default="runtime-v1")
    ap.add_argument("--name", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--file", action="append", required=True, help="可以给多个")
    a = ap.parse_args()

    token = token_of(a.token)
    if not token:
        sys.exit("没找到 token")

    # 1. 找/建 release
    st, rel = req("GET", "/repos/%s/%s/releases/tags/%s" % (a.user, a.repo, a.tag), token)
    if st != 200:
        name = a.name or a.tag
        st, rel = req("POST", "/repos/%s/%s/releases" % (a.user, a.repo), token,
                      {"tag_name": a.tag, "name": name,
                       "body": a.body or "内置运行时（大文件不走仓库，放这里）",
                       "draft": False, "prerelease": False})
        if st not in (200, 201):
            sys.exit("建 release 失败（HTTP %s: %s）" % (st, rel.get("message")))
        print("已建 release %s" % rel.get("html_url"), flush=True)
    else:
        print("release 已存在：%s" % rel.get("html_url"), flush=True)

    existing = {x["name"]: x for x in (rel.get("assets") or [])}

    # 2. 上传附件（同名先删）
    for path in a.file:
        if not os.path.isfile(path):
            print("  跳过（不存在）%s" % path)
            continue
        name = os.path.basename(path)
        if name in existing:
            print("  %s 已存在，先删旧的" % name, flush=True)
            req("DELETE", "/repos/%s/%s/releases/assets/%s"
                % (a.user, a.repo, existing[name]["id"]), token)
        mb = os.path.getsize(path) / 1024.0 / 1024.0
        print("  上传 %s（%.1f MB）…" % (name, mb), flush=True)
        t0 = time.time()
        st, r = upload_asset(a.user, a.repo, rel["id"], path, token)
        if st not in (200, 201):
            print("  FAIL %s（HTTP %s: %s）" % (name, st, r.get("message")))
            continue
        print("  ok 用时 %.0f 秒 → %s" % (time.time() - t0,
                                         r.get("browser_download_url")), flush=True)

    # 3. 打印最终下载地址
    st, rel = req("GET", "/repos/%s/%s/releases/tags/%s" % (a.user, a.repo, a.tag), token)
    print("\n下载地址：")
    for x in (rel.get("assets") or []):
        print("  %-14s %8.1f MB  %s" % (x["name"], x["size"] / 1024.0 / 1024.0,
                                        x["browser_download_url"]))


if __name__ == "__main__":
    main()
