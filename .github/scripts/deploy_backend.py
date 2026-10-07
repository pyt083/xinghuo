# -*- coding: utf-8 -*-
"""云端部署脚本（GitHub Actions 运行器执行）。

读取密钥 INSTANCE_JSON（由网站端加密写入）：{"host","port","password","service_url"}
流程：等 SSH 就绪 → 数据盘有 ComfyUI 则直接启动，否则全新部署 →
同步最新 webapp 代码 → 健康检查 → 有 service_url 则更新 backend.json。

加固要点（针对 runner→国内实例连接易断）：
  - SSH keepalive 15s
  - 所有远端操作前检查连接活性，断线自动重连
  - 部署/健康检查改为「服务器端阻塞轮询」（持续有输出，连接不空闲）
"""
import base64
import json
import os
import sys
import time
import traceback
import urllib.request

import paramiko

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = "pyt083/xinghuo"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
_cfg = {}


def log(msg):
    print(f"[deploy] {msg}", flush=True)


def connect():
    log(f"连接 {[_cfg['host'], _cfg['port']]} ...")
    cli.connect(_cfg["host"], port=_cfg["port"], username="root",
                password=_cfg["password"], timeout=25,
                look_for_keys=False, allow_agent=False,
                banner_timeout=30, auth_timeout=30)
    tr = cli.get_transport()
    tr.set_keepalive(15)
    log("SSH 已连接")


def ensure():
    tr = cli.get_transport()
    if tr is None or not tr.is_active():
        log("连接已断开，重连中...")
        connect()


def run(cmd, timeout=2400):
    ensure()
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    code = out.channel.recv_exit_status()
    if o.strip() or e.strip():
        print(o + e, flush=True)
    return code, o + e


def put(local, remote):
    ensure()
    run(f"mkdir -p $(dirname {remote})")
    sftp = cli.open_sftp()
    try:
        sftp.put(local, remote)
    finally:
        sftp.close()
    log(f"uploaded {local} -> {remote}")


def main():
    global _cfg
    _cfg = json.loads(os.environ.get("INSTANCE_JSON") or "{}")
    host = _cfg.get("host")
    port = int(_cfg.get("port") or 22)
    _cfg["port"] = port
    password = _cfg.get("password") or ""
    service_url = (_cfg.get("service_url") or "").strip()
    if not (host and password):
        print("::error::INSTANCE_JSON 缺少 host/password")
        sys.exit(1)

    # ---- 1. 等待 SSH 就绪（实例可能刚开机，最长 10 分钟）----
    connected = False
    for attempt in range(20):
        try:
            connect()
            connected = True
            break
        except Exception as e:
            log(f"等待 SSH 就绪 ({attempt + 1}/20): {e}")
            time.sleep(20)
    if not connected:
        print("::error::10 分钟内无法连接实例。请确认：① 控制台状态为「运行中」"
              "② SSH 指令/密码正确 ③ 若多次失败可能是实例所在网络限制海外访问，"
              "请改用 WorkBuddy 助手本地部署（发送密码即可）")
        sys.exit(1)

    # ---- 2. 数据盘检查：全新实例则完整部署（服务器端阻塞轮询）----
    _, out = run("[ -d /root/autodl-tmp/ComfyUI ] && echo YES || echo NO")
    if "YES" not in out:
        log("全新实例：执行完整部署（约 20-60 分钟，服务器端阻塞轮询）")
        run("mkdir -p /root/autodl-tmp/logs")
        put(os.path.join(REPO_DIR, "deploy", "deploy_h3.sh"),
            "/root/autodl-tmp/deploy_h3.sh")
        run("sed -i 's/\\r$//' /root/autodl-tmp/deploy_h3.sh && "
            "nohup bash /root/autodl-tmp/deploy_h3.sh "
            "> /root/autodl-tmp/logs/deploy.log 2>&1 < /dev/null & echo BG")
        # 服务器端阻塞轮询：每 20s 输出一行进度，连接不会空闲
        code, out = run(
            "for i in $(seq 1 180); do "
            "  grep -q DEPLOY_DONE /root/autodl-tmp/logs/deploy.log 2>/dev/null "
            "  && { echo DONE_TAG; break; }; "
            "  tail -n 1 /root/autodl-tmp/logs/deploy.log 2>/dev/null; "
            "  grep -qE '失败|BAD ' /root/autodl-tmp/logs/deploy.log 2>/dev/null "
            "  && { echo FAIL_TAG; break; }; "
            "  sleep 20; done; "
            "grep -q DONE_TAG /root/autodl-tmp/logs/deploy.log 2>/dev/null || echo STILL_RUNNING",
            timeout=4500)
        if "DONE_TAG" not in out:
            print("::error::全新部署未完成，详见上方 deploy.log 输出")
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
    run("bash /root/autodl-tmp/start_all.sh", timeout=900)

    # ---- 4. 健康检查（服务器端阻塞轮询，最长 6 分钟）----
    code, out = run(
        "for i in $(seq 1 36); do "
        "  H=$(curl -s --max-time 5 http://127.0.0.1:6006/api/health); "
        "  echo \"$H\" | grep -q '\"comfy\":true' && { echo HEALTHY; echo \"$H\"; break; }; "
        "  echo waiting-$i; sleep 10; done",
        timeout=900)
    if "HEALTHY" not in out:
        print("::error::健康检查未通过（ComfyUI 未就绪），详见上方输出")
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
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        print("::error::部署脚本异常：")
        traceback.print_exc()
        sys.exit(1)
