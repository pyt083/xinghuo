#!/bin/bash
# MiniMax H3「星火」全新实例一键部署：ComfyUI + 41GB 模型 + 校验 + 启动
# 用法：bash deploy_h3.sh   （在全新 AutoDL 实例上执行，约 20-60 分钟，视网速）
BASE=/root/autodl-tmp
LOG=$BASE/logs
PIP=/root/miniconda3/bin/pip
PY=/root/miniconda3/bin/python
mkdir -p $LOG $BASE/outputs

echo "==== [1/5] 克隆 ComfyUI ===="
if [ ! -d $BASE/ComfyUI ]; then
    cd $BASE
    for URL in https://github.com/comfyanonymous/ComfyUI.git \
               https://ghproxy.net/https://github.com/comfyanonymous/ComfyUI.git \
               https://gh-proxy.com/https://github.com/comfyanonymous/ComfyUI.git \
               https://gitclone.com/github.com/comfyanonymous/ComfyUI.git; do
        echo "尝试: $URL"
        git clone --depth 1 "$URL" ComfyUI && break
    done
fi
[ -d $BASE/ComfyUI ] || { echo "ComfyUI 克隆失败"; exit 1; }
cd $BASE/ComfyUI
$PIP install -q -r requirements.txt 2>&1 | tail -1

echo "==== [2/5] 下载模型（ModelScope，共约 41GB）===="
$PIP install -q modelscope 2>&1 | tail -1
mkdir -p $BASE/h3_download
$PY -m modelscope download --model Comfy-Org/MiniMax-H3 \
    diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
    text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
    vae/minimax_h3_video_vae_int8_convrot.safetensors \
    vae/minimax_h3_audio_vae_fp32.safetensors \
    loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors \
    --local_dir $BASE/h3_download

echo "==== [3/5] 归位模型文件 ===="
M=$BASE/ComfyUI/models
mkdir -p $M/diffusion_models $M/text_encoders $M/vae $M/loras
mv $BASE/h3_download/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors $M/diffusion_models/
mv $BASE/h3_download/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors   $M/text_encoders/
mv $BASE/h3_download/vae/minimax_h3_video_vae_int8_convrot.safetensors            $M/vae/
mv $BASE/h3_download/vae/minimax_h3_audio_vae_fp32.safetensors                    $M/vae/
mv $BASE/h3_download/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors $M/loras/

echo "==== [4/5] 完整性校验 ===="
declare -A EXPECT=(
  ["$M/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"]=20970379616
  ["$M/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"]=15687142551
  ["$M/vae/minimax_h3_video_vae_int8_convrot.safetensors"]=2811065184
  ["$M/vae/minimax_h3_audio_vae_fp32.safetensors"]=605254808
  ["$M/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"]=1956193000
)
FAIL=0
for F in "${!EXPECT[@]}"; do
    SZ=$(stat -c%s "$F" 2>/dev/null || echo 0)
    if [ "$SZ" = "${EXPECT[$F]}" ]; then
        echo "OK   $F ($SZ)"
    else
        echo "BAD  $F (实际 $SZ / 期望 ${EXPECT[$F]})"; FAIL=1
    fi
done
[ $FAIL -eq 1 ] && { echo "模型校验失败，中止"; exit 1; }

echo "==== [5/5] 启动 ComfyUI ===="
cd $BASE/ComfyUI
nohup $PY main.py --listen 0.0.0.0 --port 8188 \
    --output-directory $BASE/outputs \
    >> $LOG/comfyui.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8188 > /dev/null 2>&1 && { echo "ComfyUI 已就绪"; echo "DEPLOY_DONE"; exit 0; }
    sleep 3
done
echo "ComfyUI 启动超时"; exit 1
