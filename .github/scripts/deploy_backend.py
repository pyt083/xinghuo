# -*- coding: utf-8 -*-
"""云端部署脚本（GitHub Actions 运行器执行）。

针对 runner（海外）→ 国内 AutoDL 实例 SSH 不稳定的架构设计：
  1. 所有远端操作都是「短命令」：执行前检查连接，执行中断线则
     关闭 → 重连 → 重试（每条命令最多 4 次），单次断线不再击穿整个部署。
  2. 长耗时任务（全新部署 / 服务启动）改为服务器端 nohup 后台执行，
     云端只用「短探测」轮询状态 —— 连接中断几分钟也不影响服务器端推进。
  3. 失败原因（异常类型+消息）写入 ::error:: 首行与 GitHub Step Summary，
     在 Actions 页面即可直接看到失败原因，无需下载日志。
  4. 服务器端部署锁：以「是否有 deploy_h3.sh 进程在跑」为准，跑着就等它
     完成；锁在 finally 中兜底释放，失败不会卡死下次部署。
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


def safe_close():
    try:
        cli.close()
    except Exception:
        pass


def connect():
    safe_close()
    log(f"连接 {_cfg.get('host')}:{_cfg.get('port')} ...")
    cli.connect(_cfg["host"], port=_cfg["port"], username="root",
                password=_cfg["password"], timeout=30,
                look_for_keys=False, allow_agent=False,
                banner_timeout=60, auth_timeout=60)
    tr = cli.get_transport()
    if tr is not None:
        tr.set_keepalive(15)
    log("SSH 已连接")


def ensure():
    try:
        tr = cli.get_transport()
        if tr is not None and tr.is_active():
            return
    except Exception:
        pass
    connect()


def run(cmd, timeout=120, retries=4):
    """短命令：断线自动重连重试。禁止用于长轮询（会长时间占用连接）。"""
    last = None
    for i in range(1, retries + 1):
        try:
            ensure()
            _, out, err = cli.exec_command(cmd, timeout=timeout)
            o = out.read().decode("utf-8", errors="replace")
            e = err.read().decode("utf-8", errors="replace")
            try:
                code = out.channel.recv_exit_status()
            except Exception:
                code = -1
            if (o + e).strip():
                print((o + e).rstrip(), flush=True)
            return code, o + e
        except Exception as ex:
            last = ex
            log(f"命令中断({i}/{retries}) {type(ex).__name__}: {ex}，断线重连重试...")
            safe_close()
            time.sleep(8)
            try:
                connect()
            except Exception as cex:
                log(f"重连暂未成功: {cex}")
    raise RuntimeError(f"命令重试 {retries} 次仍失败: {type(last).__name__}: {last}")


def put(local, remote):
    last = None
    for i in range(1, 4):
        try:
            run(f"mkdir -p $(dirname '{remote}')")
            ensure()
            sftp = cli.open_sftp()
            try:
                sftp.put(local, remote)
            finally:
                sftp.close()
            log(f"uploaded {os.path.basename(local)} -> {remote}")
            return
        except Exception as ex:
            last = ex
            log(f"上传中断({i}/3) {type(ex).__name__}: {ex}，重试...")
            safe_close()
            time.sleep(8)
    raise RuntimeError(f"上传 {remote} 失败: {type(last).__name__}: {last}")


def wait_for(probe, is_done, label, attempts, interval, tolerate=15):
    """短探测轮询。probe 为快速命令；is_done(out) 判断是否完成。
    连续 tolerate 次探测失败（连接断续）才放弃，服务器端任务不受影响。"""
    broken = 0
    for i in range(1, attempts + 1):
        try:
            _, out = run(probe, timeout=60, retries=2)
        except Exception as ex:
            broken += 1
            log(f"{label}: 探测失败 {broken}/{tolerate}（{type(ex).__name__}），服务器端任务照常进行...")
            if broken >= tolerate:
                raise RuntimeError(f"{label}: 连续 {tolerate} 次探测失败，连接长时间不可用")
            time.sleep(interval)
            continue
        broken = 0
        done, detail = is_done(out)
        if done:
            return detail
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        log(f"{label} {i}/{attempts}: {tail[:120]}")
        time.sleep(interval)
    return None


def fail(msg):
    print(f"::error::{msg}".replace("\n", " ")[:700], flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"## ❌ 部署失败\n\n{msg}\n\n")
        except Exception:
            pass
    sys.exit(1)


def main_locked():
    # ---- 3. 环境检查：ComfyUI 与 5 个模型是否齐备（缺则走完整部署）----
    _, out = run(
        "if [ -d /root/autodl-tmp/ComfyUI ]; then "
        "M=/root/autodl-tmp/ComfyUI/models; ok=1; "
        "for f in diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors "
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors "
        "vae/minimax_h3_video_vae_int8_convrot.safetensors "
        "vae/minimax_h3_audio_vae_fp32.safetensors "
        "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors; do "
        "[ -s \"$M/$f\" ] || ok=0; done; "
        "[ \"$ok\" = 1 ] && echo YES || echo PARTIAL; else echo NO; fi")
    if "YES" not in out:
        log("需要完整部署（全新实例或模型不齐，约 20-60 分钟，服务器端后台进行）")
        run("mkdir -p /root/autodl-tmp/logs && rm -f /root/autodl-tmp/logs/deploy.log")
        put(os.path.join(REPO_DIR, "deploy", "deploy_h3.sh"),
            "/root/autodl-tmp/deploy_h3.sh")
        run("sed -i 's/\\r$//' /root/autodl-tmp/deploy_h3.sh && "
            "nohup bash /root/autodl-tmp/deploy_h3.sh "
            "> /root/autodl-tmp/logs/deploy.log 2>&1 < /dev/null & echo BG")

        def deploy_done(out):
            if "DEPLOY_TAG_DONE" in out:
                return True, out
            if "DEPLOY_TAG_FAIL" in out:
                fail("全新部署失败，服务器端日志片段: " + out.strip()[-600:])
            return False, out

        detail = wait_for(
            "grep -q DEPLOY_DONE /root/autodl-tmp/logs/deploy.log 2>/dev/null && echo DEPLOY_TAG_DONE; "
            "grep -qE '失败|BAD ' /root/autodl-tmp/logs/deploy.log 2>/dev/null && echo DEPLOY_TAG_FAIL; "
            "tail -n 1 /root/autodl-tmp/logs/deploy.log 2>/dev/null",
            deploy_done, "完整部署", attempts=200, interval=20)
        if detail is None:
            fail("完整部署超时（约 66 分钟未完成），请查看服务器 /root/autodl-tmp/logs/deploy.log")
        log("完整部署完成")
    else:
        log("数据盘已有 ComfyUI 和全部模型，走快速启动路径")

    # ---- 4. 同步最新后端代码 ----
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

    # ---- 5. 启动服务（服务器端 nohup，云端不占连接）----
    run("cd /root/autodl-tmp && nohup bash start_all.sh "
        "> /root/autodl-tmp/logs/start_cloud.log 2>&1 < /dev/null & echo BG")

    # ---- 6. 健康检查（短探测轮询，最长约 25 分钟）----
    def healthy(out):
        return ('"comfy":true' in out.replace(" ", "")), out

    detail = wait_for(
        "curl -s --max-time 8 http://127.0.0.1:6006/api/health 2>/dev/null; echo",
        healthy, "健康检查", attempts=100, interval=15)
    if detail is None:
        fail("健康检查超时（ComfyUI 未就绪），服务器端可查 logs/comfyui.log 与 logs/start_cloud.log")
    log("健康检查通过: ComfyUI 在线，星火后端正常")

    # ---- 7. 更新 backend.json（网站自动读取最新后端地址）----
    service_url = (_cfg.get("service_url") or "").strip()
    if service_url:
        token = os.environ["GITHUB_TOKEN"]
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        }
        content = json.dumps({"backend": service_url}, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        last_err = None
        for i in range(3):
            try:
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
                    "content": b64,
                    **({"sha": sha} if sha else {}),
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{REPO}/contents/backend.json",
                    data=body, headers=headers, method="PUT")
                with urllib.request.urlopen(req, timeout=30) as r:
                    json.load(r)
                log(f"backend.json 已更新为 {service_url}")
                last_err = None
                break
            except Exception as ex:
                last_err = ex
                log(f"backend.json 更新失败({i + 1}/3): {ex}")
                time.sleep(10)
        if last_err is not None:
            fail(f"部署成功但 backend.json 更新失败: {last_err}。"
                 f"可手动在网站把后端地址切换为 {service_url}")

    log("部署全部完成 ✅")


def main():
    global _cfg
    _cfg = json.loads(os.environ.get("INSTANCE_JSON") or "{}")
    host = _cfg.get("host")
    port = int(_cfg.get("port") or 22)
    _cfg["port"] = port
    password = _cfg.get("password") or ""
    if not (host and password):
        fail("INSTANCE_JSON 缺少 host/password，请在网站重新填写实例信息")

    # ---- 1. 等待 SSH 就绪（实例可能刚开机，最长约 10 分钟）----
    connected = False
    for attempt in range(20):
        try:
            connect()
            connected = True
            break
        except Exception as e:
            log(f"等待 SSH 就绪 ({attempt + 1}/20): {type(e).__name__}: {e}")
            time.sleep(20)
    if not connected:
        fail("10 分钟内无法连接实例。请确认：① 控制台状态为「运行中」② SSH 指令/密码正确 "
             "③ 若多次失败可能是海外 runner 无法访问实例，请改用 WorkBuddy 助手本地部署（发送密码即可）")

    # ---- 2. 部署锁：以「服务器端是否有 deploy_h3.sh 在跑」为准 ----
    def lock_free(out):
        return "ACQUIRED" in out, out

    detail = wait_for(
        "pgrep -f 'deploy_h3[.]sh' >/dev/null 2>&1 && echo BUSY || "
        "{ rm -rf /root/autodl-tmp/.deploy_lock; "
        "mkdir /root/autodl-tmp/.deploy_lock && echo ACQUIRED; }",
        lock_free, "部署锁", attempts=220, interval=20, tolerate=30)
    if detail is None:
        fail("等待其他部署完成超时（约 70 分钟），请稍后再点一次部署")

    try:
        main_locked()
    finally:
        try:
            run("rm -rf /root/autodl-tmp/.deploy_lock", timeout=60, retries=2)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"::error::部署脚本异常: {type(e).__name__}: {e}".replace("\n", " ")[:700], flush=True)
        traceback.print_exc()
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as f:
                    f.write(f"## ❌ 部署脚本异常\n\n`{type(e).__name__}: {e}`\n\n"
                            f"```\n{traceback.format_exc()}\n```\n")
            except Exception:
                pass
        sys.exit(1)
