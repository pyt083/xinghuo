# -*- coding: utf-8 -*-
"""星火（MiniMax H3）· AI 视频生成网站 — FastAPI 后端（单文件可运行）。

职责：
  - 提供中文 Web 界面（static/index.html）
  - 接收生成请求，构建 MiniMax H3 工作流并提交给 ComfyUI
  - 内存任务表 + 后台线程轮询 ComfyUI /history 更新任务状态
  - MOCK 模式（MOCK=1）：不连 ComfyUI，异步模拟生成后返回示例视频

启动：
  MOCK 模式:  set MOCK=1 && python app.py
  正式模式:  python app.py            （需 ComfyUI 已在 COMFY_URL 上运行）
  生产方式:  uvicorn app:app --host 0.0.0.0 --port 6006

AutoDL 部署：本服务监听 0.0.0.0:6006（环境变量 PORT 可改），
AutoDL 容器仅对外映射 6006 端口，直接访问映射地址即可使用。
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from comfy_bridge import ComfyBridge, ComfyBridgeError, ComfyTimeoutError
from workflow import (
    ASPECT_RATIOS,
    MAX_DURATION,
    MIN_DURATION,
    build_t2v_workflow,
    duration_to_frames,
    resolve_resolution,
)

# ---------------------------------------------------------------------------
# 全局配置（环境变量）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MOCK_SAMPLE = BASE_DIR / "mock" / "sample.mp4"

PORT = int(os.getenv("PORT", "6006"))          # AutoDL 仅对外映射 6006
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
MOCK = os.getenv("MOCK", "0").strip().lower() in ("1", "true", "yes")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "outputs")))
# 生成总超时（秒）：33B 模型在 RTX 5090 上单条 5~15s 视频预计数分钟，留足余量
GENERATION_TIMEOUT = float(os.getenv("GENERATION_TIMEOUT", "3600"))
# 任务列表最多保留条数（内存任务表，重启即清空）
TASKS_LIMIT = int(os.getenv("TASKS_LIMIT", "200"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("h3web.app")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 内存任务表（线程安全）
# ---------------------------------------------------------------------------
class TaskStore:
    """{task_id: task_dict} 的线程安全内存任务表。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, task: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._tasks[task["task_id"]] = task
            # 超出上限时丢弃最旧的已结束任务
            if len(self._tasks) > TASKS_LIMIT:
                finished = sorted(
                    (
                        t for t in self._tasks.values()
                        if t["status"] in ("done", "failed")
                    ),
                    key=lambda t: t["created_at"],
                )
                for old in finished[: len(self._tasks) - TASKS_LIMIT]:
                    self._tasks.pop(old["task_id"], None)
        return task

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(fields)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(), key=lambda t: t["created_at"], reverse=True
            )
        return tasks[:limit]


store = TaskStore()


def _public_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """任务对外的 JSON 视图（隐藏本地路径等内部字段）。"""
    view = {k: v for k, v in task.items() if k not in ("video_path", "comfy_prompt_id")}
    view["video_url"] = f"/api/video/{task['task_id']}" if task.get("video_path") else None
    return view


# ---------------------------------------------------------------------------
# 后台任务执行
# ---------------------------------------------------------------------------
def _friendly_error(exc: Exception) -> str:
    """把底层异常翻译为面向用户的友好中文提示。"""
    if isinstance(exc, ComfyTimeoutError):
        return str(exc)
    if isinstance(exc, ComfyBridgeError):
        msg = str(exc)
        if "无法连接" in msg or "Max retries" in msg or "Connection" in msg:
            return "无法连接 ComfyUI 服务，请确认 ComfyUI 已启动且地址正确"
        return f"生成失败：{msg}"
    return f"生成失败：{exc.__class__.__name__}: {exc}"


def _restart_comfyui() -> None:
    """重启 ComfyUI 以恢复被污染的显存状态（自愈机制）。"""
    import subprocess
    import urllib.request

    comfy_dir = "/root/autodl-tmp/ComfyUI"
    python_bin = "/root/miniconda3/bin/python"
    log_path = "/root/autodl-tmp/logs/comfyui.log"

    logger.info("正在重启 ComfyUI ...")
    subprocess.run(
        ["pkill", "-9", "-f", "main.py --listen 0.0.0.0 --port 8188"],
        check=False,
    )
    time.sleep(4)
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            [
                python_bin, "main.py",
                "--listen", "0.0.0.0",
                "--port", "8188",
                "--output-directory", "/root/autodl-tmp/outputs",
            ],
            cwd=comfy_dir,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # 等待 ComfyUI 就绪（最长 150 秒）
    for _ in range(50):
        time.sleep(3)
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8188/system_stats", timeout=3
            ) as resp:
                if resp.status == 200:
                    logger.info("ComfyUI 重启完成")
                    return
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("ComfyUI 重启后 150 秒内未就绪")


def _run_task(task_id: str, payload: Dict[str, Any]) -> None:
    """后台线程主体：真实模式走 ComfyUI，MOCK 模式模拟生成。"""
    try:
        store.update(task_id, status="running", started_at=time.time())
        if MOCK:
            # MOCK 模式：随机等待 3~8 秒后拷贝示例视频，用于本地 QA
            time.sleep(random.uniform(3.0, 8.0))
            if not MOCK_SAMPLE.exists():
                raise FileNotFoundError(
                    f"MOCK 示例视频不存在: {MOCK_SAMPLE}（请先按 README 生成）"
                )
            dest = OUTPUT_DIR / f"{task_id}.mp4"
            shutil.copyfile(MOCK_SAMPLE, dest)
            store.update(
                task_id,
                status="done",
                finished_at=time.time(),
                video_path=str(dest),
            )
            logger.info("[MOCK] 任务 %s 完成", task_id)
            return

        # ---- 真实模式：构建工作流并提交 ComfyUI（带自愈重试） ----
        def _execute_once() -> Path:
            graph = build_t2v_workflow(
                prompt=payload["prompt"],
                duration=payload["duration"],
                aspect=payload["aspect"],
                seed=payload["seed"],
            )
            bridge = ComfyBridge(base_url=COMFY_URL)
            prompt_id = bridge.submit(graph)
            store.update(task_id, comfy_prompt_id=prompt_id)

            def _on_poll(polls: int) -> None:
                # 简单的进度心跳：上报已等待时长
                store.update(task_id, polls=polls)

            outputs = bridge.wait_for_result(
                prompt_id,
                poll_interval=2.0,
                timeout=GENERATION_TIMEOUT,
                on_poll=_on_poll,
            )
            return bridge.download_video(outputs, OUTPUT_DIR, task_id)

        video_path: Optional[Path] = None
        for attempt in range(2):
            try:
                video_path = _execute_once()
                break
            except ComfyBridgeError as exc:
                # ComfyUI 动态显存分页（vbar）状态污染会报 "Fault failed"，
                # 自动重启 ComfyUI 恢复干净状态后重试一次。
                retryable = (
                    "Fault failed" in str(exc)
                    or "vbar" in str(exc).lower()
                    or "out of memory" in str(exc).lower()
                )
                if retryable and attempt == 0:
                    logger.warning(
                        "任务 %s 遇到 ComfyUI 显存故障（%s），自动重启 ComfyUI 后重试",
                        task_id,
                        exc,
                    )
                    _restart_comfyui()
                    continue
                raise

        store.update(
            task_id,
            status="done",
            finished_at=time.time(),
            video_path=str(video_path),
        )
        logger.info("任务 %s 完成: %s", task_id, video_path)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都必须落到任务状态
        logger.exception("任务 %s 失败", task_id)
        store.update(
            task_id,
            status="failed",
            finished_at=time.time(),
            error=_friendly_error(exc),
        )


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="星火 · AI 视频生成", docs_url=None, redoc_url=None)

# 跨域支持：允许 GitHub Pages 等独立站点调用本生成服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    """POST /api/generate 请求体。"""

    prompt: str
    duration: float = 5.0
    aspect: str = "16:9"
    seed: Optional[int] = None


@app.post("/api/generate")
def api_generate(req: GenerateRequest) -> Dict[str, Any]:
    """提交生成任务，立即返回 task_id。"""
    # ---- 参数校验（返回友好的中文错误） ----
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="提示词不能为空")
    if len(req.prompt.strip()) > 5000:
        raise HTTPException(status_code=400, detail="提示词过长（上限 5000 字符）")
    if not (MIN_DURATION <= req.duration <= MAX_DURATION):
        raise HTTPException(
            status_code=400,
            detail=f"时长需在 {MIN_DURATION:.0f}~{MAX_DURATION:.0f} 秒之间",
        )
    if req.aspect not in ASPECT_RATIOS:
        raise HTTPException(
            status_code=400,
            detail=f"画面比例需为 {', '.join(ASPECT_RATIOS)} 之一",
        )
    if req.seed is not None and not (0 <= req.seed <= 2 ** 53 - 1):
        raise HTTPException(status_code=400, detail="种子需为 0 ~ 2^53-1 之间的整数")

    width, height = resolve_resolution(
        req.aspect, frames=duration_to_frames(req.duration)
    )
    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "status": "queued",
        "prompt": req.prompt.strip(),
        "duration": req.duration,
        "aspect": req.aspect,
        "seed": req.seed,          # None 表示自动随机，实际种子以 workflow 内部生成为准
        "width": width,
        "height": height,
        "length": duration_to_frames(req.duration),  # 帧数（≡5 mod 17 对齐）
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "polls": 0,
        "mock": MOCK,
    }
    store.create(task)
    threading.Thread(
        target=_run_task,
        args=(task_id, {"prompt": req.prompt, "duration": req.duration,
                        "aspect": req.aspect, "seed": req.seed}),
        daemon=True,
        name=f"task-{task_id}",
    ).start()
    return {"task_id": task_id}


@app.get("/api/status/{task_id}")
def api_status(task_id: str) -> Dict[str, Any]:
    """查询任务状态（前端 2 秒轮询一次）。"""
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    view = _public_task(task)
    # 附带已耗时（秒），便于前端展示
    if task["status"] in ("queued", "running"):
        base = task["started_at"] or task["created_at"]
        view["elapsed"] = round(time.time() - base, 1)
    elif task.get("finished_at"):
        view["elapsed"] = round(task["finished_at"] - (task["started_at"] or task["created_at"]), 1)
    return view


@app.get("/api/tasks")
def api_tasks() -> Dict[str, Any]:
    """最近任务列表（按创建时间倒序）。"""
    return {"tasks": [_public_task(t) for t in store.list_recent(limit=50)]}


@app.get("/api/video/{task_id}")
def api_video(task_id: str) -> FileResponse:
    """返回任务产出的 mp4 文件（含音频）。"""
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if task["status"] != "done" or not task.get("video_path"):
        raise HTTPException(status_code=409, detail="任务尚未完成或没有产出视频")
    path = Path(task["video_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="视频文件已被清理")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"minimax_h3_{task_id}.mp4",
    )


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    """健康检查：ComfyUI 可达性 + GPU 信息（MOCK 模式下返回模拟数据）。"""
    result: Dict[str, Any] = {
        "mock": MOCK,
        "comfy": False,
        "comfy_url": COMFY_URL,
        "gpu": None,
    }
    if MOCK:
        # MOCK 模式：不探测 ComfyUI，返回占位 GPU 信息便于前端联调
        result["gpu"] = {
            "name": "MOCK 模式（未连接真实 GPU）",
            "type": "mock",
            "vram_total": 0,
            "vram_free": 0,
        }
        return result
    try:
        stats = ComfyBridge(base_url=COMFY_URL).get_system_stats()
        devices = stats.get("devices") or []
        result["comfy"] = True
        if devices:
            dev = devices[0]
            result["gpu"] = {
                "name": dev.get("name"),
                "type": dev.get("type"),
                "vram_total": dev.get("vram_total"),
                "vram_free": dev.get("vram_free"),
            }
    except ComfyBridgeError as exc:
        logger.warning("健康检查失败: %s", exc)
    return result


# 静态资源 + 首页
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(404)
async def not_found_handler(request, exc):  # noqa: ANN001
    return JSONResponse({"detail": getattr(exc, "detail", "资源不存在")}, status_code=404)


if __name__ == "__main__":
    logger.info(
        "启动 星火(MiniMax H3) 视频生成站 | MOCK=%s | COMFY_URL=%s | PORT=%d",
        MOCK, COMFY_URL, PORT,
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
