# -*- coding: utf-8 -*-
"""ComfyUI HTTP API 客户端（桥接层）。

负责与 ComfyUI 服务（默认 http://127.0.0.1:8188）通信：
  - POST /prompt           提交工作流，返回 prompt_id
  - GET  /history/{id}     轮询执行状态，直到 status.completed
  - GET  /view             下载生成的视频文件到本地
  - GET  /system_stats     查询 GPU / 系统信息（健康检查用）

所有异常都收敛为 ComfyBridgeError / ComfyTimeoutError，
由上层（app.py 任务线程）落到任务状态里。
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger("h3web.comfy_bridge")

# 单次 HTTP 请求的读写超时（秒）。模型加载等长操作发生在服务端，
# 轮询请求本身应当快速返回，这里只约束 HTTP 层。
HTTP_TIMEOUT = 30


class ComfyBridgeError(Exception):
    """ComfyUI 交互过程中的常规错误（不可达 / 提交被拒 / 执行失败等）。"""


class ComfyTimeoutError(ComfyBridgeError):
    """生成超时。"""


class ComfyBridge:
    """ComfyUI REST API 轻量客户端。"""

    def __init__(self, base_url: Optional[str] = None, client_id: Optional[str] = None):
        self.base_url = (base_url or "").rstrip("/") or "http://127.0.0.1:8188"
        self.client_id = client_id or str(uuid.uuid4())
        self.session = requests.Session()

    # ------------------------------------------------------------------ 基础
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """统一请求入口：连接失败等网络异常收敛为 ComfyBridgeError。"""
        url = self._url(path)
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ComfyBridgeError(f"无法连接 ComfyUI（{url}）: {exc}") from exc
        return resp

    # ------------------------------------------------------------------ 提交
    def submit(self, graph: Dict) -> str:
        """提交 API 格式工作流，返回 prompt_id。

        Raises:
            ComfyBridgeError: ComfyUI 不可达、图校验失败（node_errors）等。
        """
        payload = {"prompt": graph, "client_id": self.client_id}
        resp = self._request("POST", "/prompt", json=payload)
        if resp.status_code != 200:
            # ComfyUI 校验失败时返回 400 + {"error":..., "node_errors": {...}}
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("error") or body)
                node_errors = body.get("node_errors")
                if node_errors:
                    detail += f" | node_errors: {node_errors}"
            except ValueError:
                detail = resp.text[:500]
            raise ComfyBridgeError(
                f"ComfyUI 拒绝了工作流（HTTP {resp.status_code}）: {detail}"
            )
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyBridgeError(f"ComfyUI 未返回 prompt_id: {data}")
        logger.info("已提交工作流 prompt_id=%s", prompt_id)
        return prompt_id

    # ------------------------------------------------------------------ 轮询
    def get_history(self, prompt_id: str) -> Optional[Dict]:
        """查询某次执行的 history 记录；尚未产生记录时返回 None。"""
        resp = self._request("GET", f"/history/{prompt_id}")
        if resp.status_code != 200:
            raise ComfyBridgeError(
                f"查询 ComfyUI 历史失败（HTTP {resp.status_code}）: {resp.text[:200]}"
            )
        data = resp.json()
        # /history/{id} 返回 {prompt_id: {...}}；不存在时为 {}
        entry = data.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _extract_execution_error(entry: Dict) -> Optional[str]:
        """从 history 的 status.messages 里提取执行错误描述（无错返回 None）。"""
        status = entry.get("status") or {}
        messages = status.get("messages") or []
        for msg in messages:
            if not isinstance(msg, (list, tuple)) or len(msg) < 2:
                continue
            msg_type, body = msg[0], msg[1]
            if msg_type in ("execution_error", "execution_interrupted"):
                if isinstance(body, dict):
                    node = body.get("node_id") or body.get("node_type") or "?"
                    err = body.get("exception_message") or body.get("exception_type") or msg_type
                    return f"节点 {node}: {err}"
                return str(body)
        if status.get("status_str") == "error":
            return str(status)
        return None

    def wait_for_result(
        self,
        prompt_id: str,
        poll_interval: float = 2.0,
        timeout: float = 3600.0,
        on_poll: Optional[Callable[[int], None]] = None,
    ) -> Dict:
        """轮询直到执行完成，返回 outputs（{node_id: {...}}）。

        Args:
            poll_interval: 轮询间隔（秒）。
            timeout: 总超时（秒）。33B 模型在消费级 GPU 上可能较慢，默认 1 小时。
            on_poll: 每次轮询后的回调（参数为已轮询次数），可用于上报进度。

        Raises:
            ComfyTimeoutError: 超时未完成。
            ComfyBridgeError: ComfyUI 报告执行错误 / 无输出。
        """
        deadline = time.monotonic() + timeout
        polls = 0
        while time.monotonic() < deadline:
            entry = self.get_history(prompt_id)
            if entry is not None:
                error = self._extract_execution_error(entry)
                if error:
                    raise ComfyBridgeError(f"ComfyUI 执行出错: {error}")
                status = entry.get("status") or {}
                if status.get("completed"):
                    outputs = entry.get("outputs") or {}
                    if not outputs:
                        raise ComfyBridgeError("ComfyUI 执行完成但没有产出任何输出")
                    return outputs
            polls += 1
            if on_poll is not None:
                try:
                    on_poll(polls)
                except Exception:  # 进度回调绝不能影响主流程
                    pass
            time.sleep(poll_interval)
        raise ComfyTimeoutError(
            f"生成超时（超过 {timeout:.0f} 秒仍未完成），请稍后重试或联系管理员"
        )

    # ------------------------------------------------------------------ 产出
    @staticmethod
    def extract_video_info(outputs: Dict) -> Dict[str, str]:
        """从 outputs 中找到视频文件信息。

        兼容 SaveVideo 在不同 ComfyUI 版本下的输出结构：
          - {"gifs": [{"filename","subfolder","type"}]}（新版视频输出的历史键名）
          - {"videos": [...]} / {"images": [...]}
          - {"filenames": ["xxx.mp4", ...]}（纯文件名列表）
          - {"filename": "..."}（单个字典）
        返回 {"filename":..., "subfolder":..., "type":...}。
        """
        for node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for key in ("gifs", "videos", "images", "filenames", "video", "outputs"):
                items = node_out.get(key)
                if not items:
                    continue
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, (list, tuple)):
                    continue
                for item in items:
                    if isinstance(item, str):
                        return {"filename": item, "subfolder": "", "type": "output"}
                    if isinstance(item, dict) and item.get("filename"):
                        return {
                            "filename": str(item["filename"]),
                            "subfolder": str(item.get("subfolder") or ""),
                            "type": str(item.get("type") or "output"),
                        }
        raise ComfyBridgeError(
            "未能从 ComfyUI 输出中解析到视频文件"
            f"（outputs 结构: {str(outputs)[:300]}）"
        )

    def download_video(
        self,
        outputs: Dict,
        dest_dir: Path,
        task_id: str,
    ) -> Path:
        """把 ComfyUI 产出的视频下载到本地，返回文件路径。

        Args:
            outputs: wait_for_result 返回的 outputs。
            dest_dir: 目标目录（不存在则自动创建）。
            task_id: 任务 ID，用于命名本地文件 {task_id}.mp4。
        """
        info = self.extract_video_info(outputs)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{task_id}.mp4"
        params = {
            "filename": info["filename"],
            "subfolder": info["subfolder"],
            "type": info["type"],
        }
        resp = self._request("GET", "/view", params=params, stream=True, timeout=300)
        if resp.status_code != 200:
            raise ComfyBridgeError(
                f"下载视频失败（HTTP {resp.status_code}）: {info['filename']}"
            )
        try:
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        finally:
            resp.close()
        if dest.stat().st_size < 1024:
            dest.unlink(missing_ok=True)
            raise ComfyBridgeError("下载的视频文件过小，疑似损坏")
        logger.info("视频已下载: %s", dest)
        return dest

    # ------------------------------------------------------------------ 健康
    def get_system_stats(self) -> Dict[str, Any]:
        """GET /system_stats：返回系统与 GPU 设备信息。"""
        resp = self._request("GET", "/system_stats")
        if resp.status_code != 200:
            raise ComfyBridgeError(
                f"查询 /system_stats 失败（HTTP {resp.status_code}）"
            )
        return resp.json()
