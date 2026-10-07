# -*- coding: utf-8 -*-
"""MiniMax H3 文生视频 ComfyUI 工作流构建模块。

将 deploy/workflow_t2v_template.json（官方 ComfyUI T2V 模板，核心图位于
definitions.subgraphs[0]，共 21 个节点）转换为 ComfyUI **API 格式**的扁平图：
    {node_id: {"class_type": ..., "inputs": {...}}}

转换原则（忠实还原模板的连线关系与默认参数）：
  1. 文生视频（t2va）模式下，MiniMaxH3ImageToVideo 的 first_frame / last_frame
     输入为空（模板外层节点 140 的 first_frame/last_frame link 为 null，
     即 0 张图 -> t2va 纯文生视频模式），因此 API 图中直接省略这两个输入。
  2. 模板中的 ComfySwitchNode（Model/Steps 两处 If/Else 开关）由
     PrimitiveBoolean(turbo_mode) 控制。本站固定启用 Turbo 模式
     （外层节点 140 的 lora_name = 8 步 Turbo LoRA、strength_model = 1、
     turbo_steps = 8），因此静态消解开关：
        MODEL 路径：UNETLoader -> LoraLoaderModelOnly -> BasicScheduler/BasicGuider
        steps 路径：直接使用 turbo_steps = 8
     说明：模板 BasicScheduler 节点残留的 widget 值 steps=4 是被
     link 235（switch 输出）覆盖的陈旧值，实际生效的是 turbo_steps=8
     （与 8 步 Turbo LoRA 配套）。如需改回 4 步，请同步更换 4 步 LoRA。
  3. 模板帧长由 ComfyMathExpression 节点计算：
        max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
     其中 a 为时长（秒）、24 为 fps。该表达式把帧数向上对齐到
     "17k+5"（模 17 余 5）的块网格上。本模块用 duration_to_frames()
     等价实现（JS 的 Math.round 为四舍五入，Python 用 floor(x+0.5) 模拟）。
  4. 分辨率规则（ResolutionSelector + 官方说明）：短边固定 768，
     长边按画面比例推算、四舍五入到 32 的倍数，且上限 1344x768：
        16:9 -> 1344x768   9:16 -> 768x1344   1:1 -> 768x768
     （对应模板 megapixels=0.98 / multiple=32 的官方 768p 档位。）
"""

from __future__ import annotations

import math
import os
import random
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 模型文件名（与模板 widgets_values_named 完全一致，勿随意改动）
# ---------------------------------------------------------------------------
UNET_NAME = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE_NAME = "minimax_h3_video_vae_int8_convrot.safetensors"
AUDIO_VAE_NAME = "minimax_h3_audio_vae_fp32.safetensors"
LORA_NAME = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"

# ---------------------------------------------------------------------------
# 采样参数（模板 Turbo 模式默认值）
# ---------------------------------------------------------------------------
FPS = 24                    # CreateVideo 节点 fps
BIT_DEPTH = 8               # CreateVideo 节点 bit_depth
SCHEDULER = "simple"        # BasicScheduler
SAMPLER_NAME = "res_multistep"  # KSamplerSelect
DENOISE = 1.0               # BasicScheduler
LORA_STRENGTH = 1.0         # LoraLoaderModelOnly strength_model
FILENAME_PREFIX = "video/MiniMax_H3"  # SaveVideo 输出前缀

# Turbo 采样步数：模板 turbo_steps=8（与 8 步 Turbo LoRA 配套）。
# 可通过环境变量 H3_SAMPLER_STEPS 覆盖（例如换成 4 步 LoRA 时设为 4）。
try:
    SAMPLER_STEPS = int(os.getenv("H3_SAMPLER_STEPS", "8"))
except ValueError:
    SAMPLER_STEPS = 8

# ---------------------------------------------------------------------------
# 分辨率规则常量（短边 768 / 32 倍数 / 上限 1344）
# ---------------------------------------------------------------------------
SHORT_EDGE = 768
MAX_LONG_EDGE = 1344
ALIGN_MULTIPLE = 32

# 支持的画面比例 -> (宽比, 高比)。前端/后端共用这一集合。
ASPECT_RATIOS: Dict[str, Tuple[int, int]] = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
}

# 时长允许范围（秒）。模型官方支持约 15 秒以内。
MIN_DURATION = 4.0
MAX_DURATION = 15.0

# 种子上限（JS Number 安全整数范围，与 ComfyUI seed 控件一致）
_SEED_MAX = 2 ** 53 - 1


def _round_half_up(x: float) -> int:
    """模拟 JS Math.round 的四舍五入（.5 向上）。"""
    return int(math.floor(x + 0.5))


def _align_to_multiple(value: int, multiple: int = ALIGN_MULTIPLE) -> int:
    """把整数向上对齐到 multiple 的倍数（分辨率规则要求 32 倍数）。"""
    return int(math.ceil(value / multiple) * multiple)


def duration_to_frames(duration: float) -> int:
    """把时长（秒）转换为模型合法的帧数。

    等价实现模板中 ComfyMathExpression 的表达式：
        max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
    即：帧数 = 24fps 换算值向上对齐到 ≡5 (mod 17) 的网格（17 帧一块的
    模型时间网格）。示例：5s->124、8s->192、10s->243、15s->362。
    """
    duration = float(duration)
    base = max(5, _round_half_up(duration * FPS))
    remainder = base % 17
    snap = (5 - remainder) % 17
    return base + snap


def _floor_to_multiple(value: int, multiple: int = ALIGN_MULTIPLE) -> int:
    """把整数向下对齐到 multiple 的倍数（自适应降分辨率用）。"""
    return max(multiple, int(value // multiple) * multiple)


# 经验显存预算：32GB 显存（RTX 5090）上 768p 全分辨率实测可稳定生成的
# 帧数上限（5 秒=124 帧全分辨率通过；15 秒=362 帧全分辨率必然 OOM）。
# 超过该帧数时按「帧数 × 像素总量」守恒自动降低分辨率。
_SAFE_FRAMES_FULL_RES = 150


def resolve_resolution(aspect: str, frames: Optional[int] = None) -> Tuple[int, int]:
    """根据画面比例（及帧数）计算 (width, height)。

    规则：短边固定 768；长边 = 768 * 长短比，向上对齐到 32 的倍数，
    并被限制在 1344 以内；短边本身也是 32 的倍数（768 = 32*24）。

    自适应规则：当帧数超过 _SAFE_FRAMES_FULL_RES 时，按
    「帧数 × 像素总量 ≈ 预算」守恒缩小短边（下限 448），避免长时长
    视频在 32GB 显存上采样 OOM。例如 16:9：10s -> 1024x576、15s -> 864x480。
    """
    if aspect not in ASPECT_RATIOS:
        raise ValueError(f"不支持的画面比例: {aspect!r}（可选: {', '.join(ASPECT_RATIOS)}）")
    wr, hr = ASPECT_RATIOS[aspect]
    long_ratio = max(wr, hr) / min(wr, hr)
    short = SHORT_EDGE
    if frames is not None and frames > _SAFE_FRAMES_FULL_RES:
        budget = _SAFE_FRAMES_FULL_RES * SHORT_EDGE * MAX_LONG_EDGE
        max_short = int(math.sqrt(budget / (frames * long_ratio)))
        short = max(448, min(SHORT_EDGE, _floor_to_multiple(max_short)))
    long_edge = min(MAX_LONG_EDGE, _align_to_multiple(int(math.ceil(short * long_ratio))))
    # 宽比高 -> 宽为长边；否则高为长边
    if wr >= hr:
        return long_edge, short
    return short, long_edge


def build_t2v_workflow(
    prompt: str,
    duration: float,
    aspect: str = "16:9",
    seed: Optional[int] = None,
) -> Dict:
    """构建 MiniMax H3 文生视频（t2va）的 ComfyUI API 格式工作流图。

    Args:
        prompt:   完整提示词（建议包含镜头/运镜/音频描述，见官方 prompt 指南）。
        duration: 时长（秒），4~15。
        aspect:   画面比例，见 ASPECT_RATIOS。
        seed:     随机种子；None 时自动随机。

    Returns:
        ComfyUI API 格式 dict：{node_id: {"class_type": ..., "inputs": {...}}}

    Raises:
        ValueError: 参数非法（提示词为空 / 时长越界 / 比例不支持）。
    """
    # ---- 参数校验 ----
    if not prompt or not prompt.strip():
        raise ValueError("提示词不能为空")
    prompt = prompt.strip()
    duration = float(duration)
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise ValueError(f"时长需在 {MIN_DURATION:.0f}~{MAX_DURATION:.0f} 秒之间")
    if seed is None:
        seed = random.randrange(0, _SEED_MAX)
    seed = int(seed)
    if not (0 <= seed <= _SEED_MAX):
        raise ValueError(f"种子需在 0 ~ {_SEED_MAX} 之间")

    length = duration_to_frames(duration)
    width, height = resolve_resolution(aspect, frames=length)

    # ---- 节点连线（与模板 subgraph links 一一对应）----
    # 节点 ID 沿用模板编号，便于对照排查。
    graph: Dict = {
        # 模型加载 ----------------------------------------------------------------
        # 127 UNETLoader：加载 H3 主干（int8 量化）
        "127": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": UNET_NAME,
                "weight_dtype": "default",
            },
        },
        # 128 CLIPLoader：加载 Qwen3-VL 文本编码器（type=minimax 为 H3 专用）
        "128": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": CLIP_NAME,
                "type": "minimax",
                "device": "default",
            },
        },
        # 134 LoraLoaderModelOnly：Turbo LoRA（加速采样，strength=1）
        "134": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["127", 0],
                "lora_name": LORA_NAME,
                "strength_model": LORA_STRENGTH,
            },
        },
        # 119 VAELoader：视频 VAE（int8）
        "119": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": VIDEO_VAE_NAME},
        },
        # 120 VAELoader：音频 VAE（fp32，负责立体声音频解码）
        "120": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": AUDIO_VAE_NAME},
        },
        # 条件与潜空间初始化 --------------------------------------------------------
        # 131 MiniMaxH3ImageToVideo：t2va 核心。文生视频时不连接
        #    first_frame/last_frame（0 张图 => 纯文生视频模式）。
        #    输出 slot0=positive(CONDITIONING)、slot1=LATENT。
        "131": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["128", 0],
                "vae": ["119", 0],
                "prompt": prompt,
                "width": width,
                "height": height,
                "length": length,  # 帧数已按 ≡5 (mod 17) 网格对齐
            },
        },
        # 采样 ----------------------------------------------------------------
        # 123 KSamplerSelect：res_multistep 采样器
        "123": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": SAMPLER_NAME},
        },
        # 124 BasicScheduler：simple 调度（turbo 模式 8 步）
        "124": {
            "class_type": "BasicScheduler",
            "inputs": {
                "model": ["134", 0],
                "scheduler": SCHEDULER,
                "steps": SAMPLER_STEPS,
                "denoise": DENOISE,
            },
        },
        # 129 RandomNoise：随机噪声种子
        "129": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
        # 126 BasicGuider：模型 + 正向条件
        "126": {
            "class_type": "BasicGuider",
            "inputs": {
                "model": ["134", 0],
                "conditioning": ["131", 0],
            },
        },
        # 125 SamplerCustomAdvanced：noise/guider/sampler/sigmas/latent_image 五输入
        "125": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["129", 0],
                "guider": ["126", 0],
                "sampler": ["123", 0],
                "sigmas": ["124", 0],
                "latent_image": ["131", 1],
            },
        },
        # 解码与封装 ------------------------------------------------------------
        # 122 VAEDecode：视频潜空间 -> 图像序列
        "122": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["125", 0],
                "vae": ["119", 0],
            },
        },
        # 121 VAEDecodeAudio：音频潜空间 -> 立体声音频
        "121": {
            "class_type": "VAEDecodeAudio",
            "inputs": {
                "samples": ["125", 0],
                "vae": ["120", 0],
            },
        },
        # 130 CreateVideo：图像 + 音频 -> VIDEO（24fps）
        "130": {
            "class_type": "CreateVideo",
            "inputs": {
                "images": ["122", 0],
                "audio": ["121", 0],
                "fps": FPS,
                "bit_depth": BIT_DEPTH,
            },
        },
        # 92 SaveVideo：保存到 ComfyUI output/video/MiniMax_H3*
        "92": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["130", 0],
                "filename_prefix": FILENAME_PREFIX,
                "format": "auto",
                "codec": "auto",
            },
        },
    }
    return graph
