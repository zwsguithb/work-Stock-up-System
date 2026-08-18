#!/usr/bin/env python3
"""通过 GitHub Contents API 把当前 git 跟踪的文件推送到仓库。

为什么用这个脚本而不是直接 `git push`：
本机 git 的 HTTPS Basic 鉴权被 GitHub 拒绝（同一 PAT 用 Bearer 调 API 正常，
但 git push/ls-remote 报 "Password authentication is not supported"）。
此脚本改用 Contents API（Bearer 头 + base64 内容），在受限环境同样可用。

用法：
    GITHUB_TOKEN=ghp_xxx python deploy_api.py
    GITHUB_TOKEN=ghp_xxx python deploy_api.py --owner zwsguithb --repo work-Stock-up-System

说明：
- 仅推送 `git ls-files` 跟踪的文件（已通过 .gitignore 排除 data/ 与调试脚本）。
- 已存在的文件会先取 sha 再更新；不存在则新建。
- 每次运行会在 GitHub 上为每个变更文件生成一次提交（main 分支）。
"""
import os
import sys
import json
import base64
import argparse
import subprocess
import urllib.request
import urllib.error
import urllib.parse

API = "https://api.github.com"


def git_config(key, default=""):
    try:
        return subprocess.check_output(
            ["git", "config", key], stderr=subprocess.DEVNULL
        ).decode().strip() or default
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default=os.environ.get("GITHUB_USER") or git_config("user.name") or "zwsguithb")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO") or "work-Stock-up-System")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default=None)
    args = ap.parse_args()

    TOKEN = os.environ.get("GITHUB_TOKEN")
    if not TOKEN:
        print("ERROR: 请设置环境变量 GITHUB_TOKEN", file=sys.stderr)
        raise SystemExit(1)

    AUTH = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "workbuddy-deploy",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    NAME = git_config("user.name", "zwsguithb")
    EMAIL = git_config("user.email", "zwsguithb@users.noreply.github.com")

    def api(method, path, data=None):
        url = f"{API}{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=AUTH, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    files = subprocess.check_output(["git", "ls-files"]).decode().splitlines()
    print(f"files to push: {len(files)} -> {args.owner}/{args.repo} ({args.branch})")

    for i, f in enumerate(files):
        with open(f, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        path = urllib.parse.quote(f, safe="")
        message = args.message or f"deploy: update {f}"
        payload = {
            "message": message,
            "content": b64,
            "branch": args.branch,
            "author": {"name": NAME, "email": EMAIL},
            "committer": {"name": NAME, "email": EMAIL},
        }
        # 已存在则取 sha 更新
        st_get, existing = api("GET", f"/repos/{args.owner}/{args.repo}/contents/{path}?ref={args.branch}")
        if st_get == 200 and isinstance(existing, dict) and existing.get("sha"):
            # 内容未变则跳过，避免无意义的提交
            if existing.get("content") == b64:
                print(f"  [{i+1}/{len(files)}] skip (unchanged) {f}")
                continue
            payload["sha"] = existing["sha"]
        st, res = api("PUT", f"/repos/{args.owner}/{args.repo}/contents/{path}", payload)
        if st not in (200, 201):
            print("PUT FAIL", f, st, res)
            raise SystemExit(1)
        verb = "update" if payload.get("sha") else "create"
        print(f"  [{i+1}/{len(files)}] {verb} {f} -> {res.get('commit', {}).get('sha', '')[:8]}")

    print("DONE. REPO URL: https://github.com/" + args.owner + "/" + args.repo)


if __name__ == "__main__":
    main()
