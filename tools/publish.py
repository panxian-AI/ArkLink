"""publish.py — 用 GitHub Git Data API 把当前 HEAD 推到远程（git push 被代理阻断时用）。

流程：创建仓库（幂等）→ 全量上传 blob → 建树 → 提交（首个提交无 parent）→ 更新 ref。

用法::

    python tools/publish.py <token> [owner/repo] [branch]
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "panxian-AI/ArkLink"
ROOT = "https://api.github.com"


def api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{ROOT}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "arklink-publish",
        },
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def ensure_repo(token: str, repo: str) -> None:
    try:
        api("GET", f"/repos/{repo}", token)
        print(f"仓库已存在: {repo}")
        return
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
    api("POST", "/user/repos", token, {
        "name": repo.split("/", 1)[-1],
        "description": "ArkBot 协议外挂层：Milky / Satori 通道 + QQ NT 原生协议逆向工具箱",
        "private": False,
        "has_issues": True,
        "has_wiki": False,
    })
    print(f"已创建公开仓库: {repo}")


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        print("用法: python tools/publish.py <token> [owner/repo] [branch]")
        return 2
    repo = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_REPO
    branch = sys.argv[3] if len(sys.argv) > 3 else "main"
    message = git("log", "-1", "--format=%B")

    ensure_repo(token, repo)

    parent = ""
    try:
        parent = api("GET", f"/repos/{repo}/commits/{branch}", token)["sha"]
        print(f"远程 {branch}: {parent[:7]}")
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
        print(f"远程 {branch} 为空，将创建首个提交")

    entries = []
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, otype, sha = meta.split()
        if otype != "blob":
            continue
        content = subprocess.run(
            ["git", "cat-file", "blob", sha], capture_output=True, check=True
        ).stdout
        blob = api("POST", f"/repos/{repo}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        assert blob["sha"] == sha, f"blob 不一致: {path}"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    print(f"blob 上传完成: {len(entries)} 个")

    tree = api("POST", f"/repos/{repo}/git/trees", token, {"tree": entries})
    payload = {"message": message, "tree": tree["sha"]}
    if parent:
        payload["parents"] = [parent]
    commit = api("POST", f"/repos/{repo}/git/commits", token, payload)

    path = f"/repos/{repo}/git/refs/heads/{branch}"
    if parent:
        api("PATCH", path, token, {"sha": commit["sha"], "force": True})
    else:
        api("POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    print(f"推送成功: {commit['sha'][:7]} → {repo}@{branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
