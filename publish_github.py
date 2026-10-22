"""把目录里的文件同步到 GitHub 仓库（只用 REST API，不需要装 git）。

用法：
    python publish_github.py --user CSCBDD392 --repo danmuji
    python publish_github.py --user CSCBDD392 --repo danmuji --dry-run

token 来源：--token 参数 → GITHUB_TOKEN / GH_TOKEN 环境变量 → 当前目录 .github-token
**不要把 token 写进仓库或日志。**
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
# 不上传的东西
SKIP_NAMES = {".git", "__pycache__", ".github-token", "config.local.json", ".pkgcache"}
SKIP_EXT = {".pyc", ".log", ".tmp"}
# GitHub 单文件硬上限 100MB，留点余量，超了就跳过并提示
MAX_BYTES = 95 * 1024 * 1024
# 走 Release 而不是仓库的大文件（Git blob API 会拒："input was too large"）
RELEASE_ONLY = {"vendor/kugou-api/node.exe"}

DESC = "B 站弹幕点歌机 + 正方形弹幕面板：弹幕点歌 → 三源聚合播放；弹幕实时投影到任意窗口/屏幕旁"


def token_of(explicit=""):
    for v in (explicit, os.environ.get("GITHUB_TOKEN"), os.environ.get("GH_TOKEN")):
        if v:
            return v.strip()
    p = os.path.join(HERE, ".github-token")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def req(method, path, token, data=None, timeout=180, tries=3):
    url = path if path.startswith("http") else API + path
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for attempt in range(tries):
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
        except (urllib.error.URLError, OSError) as e:
            # 大文件（node.exe 100MB+）传一半可能超时，退避后重试
            last = e
            if attempt < tries - 1:
                time.sleep(2 + attempt * 3)
    raise last


def blob_sha(data):
    """git blob sha，用来判断文件有没有变。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def files_to_sync(root, verbose=False):
    out, skipped = [], []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_NAMES and not x.startswith("_dbg")]
        for n in names:
            if n in SKIP_NAMES:
                continue
            if n.startswith("_") and n != "_gen_page.py":
                continue
            if os.path.splitext(n)[1].lower() in SKIP_EXT:
                continue
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if rel.startswith("."):
                continue
            size = os.path.getsize(p)
            if rel in RELEASE_ONLY or size > MAX_BYTES:
                skipped.append((rel, size))
                continue
            with open(p, "rb") as f:
                out.append((rel, f.read()))
    out.sort()
    if skipped:
        print("跳过 %d 个超过 %d MB 的文件：" % (len(skipped), MAX_BYTES // 1024 // 1024))
        for rel, size in skipped:
            print("  %-50s %.1f MB" % (rel, size / 1024.0 / 1024))
    if verbose:
        for rel, b in out:
            print("  %-50s %8d B" % (rel, len(b)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="GitHub 用户名")
    ap.add_argument("--repo", default="danmuji")
    ap.add_argument("--token", default="")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="danmuji: update")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="打印每个文件")
    a = ap.parse_args()

    files = files_to_sync(HERE, a.verbose)
    total = sum(len(b) for _, b in files)
    print("待上传 %d 个文件，共 %.1f MB" % (len(files), total / 1024.0 / 1024), flush=True)
    if a.dry_run:
        return

    token = token_of(a.token)
    if not token:
        sys.exit("没找到 token：设 GITHUB_TOKEN 环境变量，或写进 .github-token")

    st, me = req("GET", "/user", token)
    if st != 200:
        sys.exit("token 无效（HTTP %s: %s）" % (st, me.get("message")))
    print("已登录：%s" % me.get("login"), flush=True)

    st, r = req("GET", "/repos/%s/%s" % (a.user, a.repo), token)
    if st == 404:
        st2, r = req("POST", "/user/repos", token,
                     {"name": a.repo, "description": DESC, "private": bool(a.private),
                      "has_issues": True, "has_wiki": False})
        if st2 not in (200, 201):
            sys.exit("建仓库失败（HTTP %s: %s）" % (st2, r.get("message")))
        print("已新建仓库 %s" % r["html_url"], flush=True)
    else:
        print("仓库已存在：%s" % r.get("html_url"), flush=True)
        # 简介可能过时，顺手更新
        req("PATCH", "/repos/%s/%s" % (a.user, a.repo), token, {"description": DESC})

    st, r = req("GET", "/repos/%s/%s/git/ref/heads/%s" % (a.user, a.repo, a.branch), token)
    if st == 200:
        ref = r["object"]["sha"]
        print("提交到已有分支 %s（%s）" % (a.branch, ref[:8]), flush=True)
    else:
        # 空仓库：Git Data API 建 blob 会 409，先用 Contents API 放个 README
        ref = None
        st2, r2 = req("PUT", "/repos/%s/%s/contents/README.md" % (a.user, a.repo), token,
                      {"message": "init",
                       "content": base64.b64encode(b"# init\n").decode(),
                       "branch": a.branch})
        if st2 not in (200, 201):
            sys.exit("初始化空仓库失败（HTTP %s: %s）" % (st2, r2.get("message")))
        print("已初始化空仓库", flush=True)
        st, r = req("GET", "/repos/%s/%s/git/ref/heads/%s" % (a.user, a.repo, a.branch), token)
        ref = r["object"]["sha"] if st == 200 else None

    # 已有文件（git blob sha），相同就跳过
    existing = {}
    if ref:
        st, tree = req("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (a.user, a.repo, ref),
                       token)
        if st == 200:
            existing = {t["path"]: t["sha"] for t in tree.get("tree", [])
                        if t["type"] == "blob"}

    todo = [(rel, data) for rel, data in files if existing.get(rel) != blob_sha(data)]
    print("要传 %d 个，未变 %d 个" % (len(todo), len(files) - len(todo)), flush=True)
    if not todo:
        print("没有变化，收工。")
        return

    # Git Data API：blob → tree → commit → 更新 ref，一次性原子提交
    entries, done = [], 0
    for rel, data in todo:
        print("  上传 %-52s %7.1f KB" % (rel, len(data) / 1024.0), end="", flush=True)
        st, r = req("POST", "/repos/%s/%s/git/blobs" % (a.user, a.repo), token,
                    {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if st not in (200, 201):
            print("  FAIL %s" % r.get("message"))
            continue
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": r["sha"]})
        done += 1
        print("  ok")

    if not entries:
        sys.exit("没有文件上传成功")

    # 一次提交 1000 个文件的 tree 会被 GitHub 拒（HTTP 422 timed out），
    # 所以分批：每批都在前一批的 tree 上叠，最后再建 commit。
    print("建 tree（%d 个文件，分批）…" % len(entries), flush=True)
    tree_sha = None
    BATCH = 200
    for i in range(0, len(entries), BATCH):
        chunk = entries[i:i + BATCH]
        body = {"tree": chunk}
        if tree_sha:
            body["base_tree"] = tree_sha
        elif ref:
            body["base_tree"] = ref
        st, r = req("POST", "/repos/%s/%s/git/trees" % (a.user, a.repo), token, body)
        if st not in (200, 201):
            sys.exit("建 tree 失败（第 %d 批，HTTP %s: %s）"
                     % (i // BATCH + 1, st, r.get("message")))
        tree_sha = r["sha"]
        print("  %d/%d" % (min(i + BATCH, len(entries)), len(entries)), flush=True)

    print("建 commit…", flush=True)
    st, r = req("POST", "/repos/%s/%s/git/commits" % (a.user, a.repo), token,
                {"message": a.message, "tree": tree_sha,
                 "parents": [ref] if ref else []})
    if st not in (200, 201):
        sys.exit("建 commit 失败（HTTP %s: %s）" % (st, r.get("message")))
    commit = r["sha"]

    if ref:
        st, r = req("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (a.user, a.repo, a.branch),
                    token, {"sha": commit, "force": True})
    else:
        st, r = req("POST", "/repos/%s/%s/git/refs" % (a.user, a.repo), token,
                    {"ref": "refs/heads/" + a.branch, "sha": commit})
    if st not in (200, 201):
        sys.exit("更新分支失败（HTTP %s: %s）" % (st, r.get("message")))

    print("完成：%d 个文件 → 提交 %s → https://github.com/%s/%s"
          % (done, commit[:8], a.user, a.repo))


if __name__ == "__main__":
    main()
