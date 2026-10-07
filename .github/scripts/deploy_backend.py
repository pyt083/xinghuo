# -*- coding: utf-8 -*-
"""云端部署脚本（GitHub Actions 运行器执行）。

读取密钥 INSTANCE_JSON（由网站端加密写入）：{"host","port","password","service_url"}
流程：等 SSH 就绪 → 数据盘有 ComfyUI 则直接启动，否则全新部署 →
同步最新 webapp 代码 → 健康检查 → 有 service_url 则更新 backend.json。
"""
import base64
import json
import os
import sys
import time
import urllib.request

import paramiko

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = "pyt083/xinghuo"


def log(msg):
    print(f"[deploy] {msg}")


def main():
    cfg = json.loads(os.environ.get("INSTANCE_JSON") or "{}")
    host = cfg.get("host")
    port = int(cfg.get("port") or 22)
    password = cfg.get("password") or ""
    service_url = (cfg.get("service_url") or "").strip()
    if not (host and password):
        print("::error::INSTANCE_JSON 缺少 host/password")
        sys.exit(1)

    # ---- 1. 等待 SSH 就绪（实例可能刚开机，最长 10 分钟）----
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connected = False
    for attempt in range(20):
        try:
            cli.connect(host, port=port, username="root", password=password,
                        timeout=20, look_for_keys=False, allow_agent=False)
            connected = True
            break
        except Exception as e:
            log(f"等待 SSH 就绪 ({attempt + 1}/20): {e}")
            time.sleep(30)
    if not connected:
        print("::error::10 分钟内无法连接实例，请确认控制台状态为「运行中」")
        sys.exit(1)

    def run(cmd, timeout=2400):
        _, out, err = cli.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", errors="replace")
        e = err.read().decode("utf-8", errors="replace")
        code = out.channel.recv_exit_status()
        print(f"$ {cmd}\n{o}{e}")
        return code, o + e

    def put(local, remote):
        run(f"mkdir -p $(dirname {remote})")
        sftp.put(local, remote)
        log(f"uploaded {local} -> {remote}")

    sftp = cli.open_sftp()

    # ---- 2. 数据盘检查：全新实例则完整部署 ----
    _, out = run("[ -d /root/autodl-tmp/ComfyUI ] && echo YES || echo NO")
    if "YES" not in out:
        log("全新实例：执行完整部署（约 20-60 分钟）")
        run("mkdir -p /root/autodl-tmp/logs")
        put(os.path.join(REPO_DIR, "deploy", "deploy_h3.sh"),
            "/root/autodl-tmp/deploy_h3.sh")
        run("sed -i 's/\\r$//' /root/autodl-tmp/deploy_h3.sh && "
            "nohup bash /root/autodl-tmp/deploy_h3.sh "
            "> /root/autodl-tmp/logs/deploy.log 2>&1 < /dev/null & echo BG")
        done = False
        for i in range(120):  # 最长 60 分钟
            time.sleep(30)
            _, out = run("tail -n 2 /root/autodl-tmp/logs/deploy.log 2>/dev/null; "
                         "grep -q DEPLOY_DONE /root/autodl-tmp/logs/deploy.log 2>/dev/null "
                         "&& echo DONE_TAG || true")
            if "DONE_TAG" in out:
                done = True
                break
            if i % 4 == 0:
                log(f"部署进行中… ({(i + 1) * 30}s)")
        if not done:
            print("::error::全新部署超时，请查看实例上 /root/autodl-tmp/logs/deploy.log")
            sys.exit(1)
    else:
        log("数据盘已有 ComfyUI，跳过部署")

    # ---- 3. 同步最新后端代码 ----
    for local, remote in [
        ("webapp/app.py", "/root/autodl-tmp/webapp/app.py"),
        ("webapp/comfy_bridge.py", "/root/autodl-tmp/webapp/comfy_bridge.py"),
        ("webapp/workflow.py", "/root/autodl-tmp/webapp/workflow.py"),
        ("webapp/static/index.html", "/root/autodl-tmp/webapp/static/index.html"),
        ("deploy/start_all.sh", "/root/autodl-tmp/start_all.sh"),
    ]:
        put(os.path.join(REPO_DIR, local), remote)
    run("sed -i 's/\\r$//' /root/autodl-tmp/start_all.sh && "
        "chmod +x /root/autodl-tmp/start_all.sh")
    run("/root/miniconda3/bin/python -c 'import fastapi, uvicorn, requests' 2>/dev/null || "
        "/root/miniconda3/bin/pip install -q fastapi uvicorn requests")
    run("pkill -9 -f '/root/miniconda3/bin/python app[.]py' 2>/dev/null; true")
    time.sleep(2)
    run("bash /root/autodl-tmp/start_all.sh", timeout=600)

    # ---- 4. 健康检查（最多等 3 分钟）----
    healthy = False
    for _ in range(18):
        time.sleep(10)
        _, out = run("curl -s --max-time 5 http://127.0.0.1:6006/api/health")
        if '"comfy":true' in out:
            healthy = True
            break
    if not healthy:
        print("::error::健康检查未通过（ComfyUI 可能仍在加载，可稍后手动验证）")
        sys.exit(1)
    log("健康检查通过：ComfyUI 在线，星火后端正常")

    # ---- 5. 更新 backend.json（网站自动读取最新后端地址）----
    if service_url:
        token = os.environ["GITHUB_TOKEN"]
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        }
        content = json.dumps({"backend": service_url}, ensure_ascii=False, indent=2)
        # 读取现有文件 sha（若存在）
        sha = None
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/contents/backend.json",
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                sha = json.load(r)["sha"]
        except Exception:
            pass
        body = json.dumps({
            "message": "chore: 云端部署自动切换后端地址",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            **({"sha": sha} if sha else {}),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/contents/backend.json",
            data=body, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            json.load(r)
        log(f"backend.json 已更新为 {service_url}")

    log("部署全部完成 ✅")


if __name__ == "__main__":
    main()
