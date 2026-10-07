#!/bin/bash
# 星火全栈一键启动脚本（实例重启后执行）
LOG=/root/autodl-tmp/logs

# 1. 启动 ComfyUI（默认动态显存模式；后端 app.py 自带故障自愈重启逻辑）
if ! curl -s --max-time 3 http://127.0.0.1:8188 > /dev/null 2>&1; then
    cd /root/autodl-tmp/ComfyUI
    nohup /root/miniconda3/bin/python main.py --listen 0.0.0.0 --port 8188 \
        --output-directory /root/autodl-tmp/outputs \
        >> $LOG/comfyui.log 2>&1 < /dev/null &
    echo "ComfyUI 启动中..."
    for i in $(seq 1 60); do
        curl -s --max-time 3 http://127.0.0.1:8188 > /dev/null 2>&1 && { echo "ComfyUI OK (等待${i}x3秒)"; break; }
        sleep 3
    done
else
    echo "ComfyUI 已在运行"
fi

# 2. 启动星火后端 (端口 6006)
if ! curl -s --max-time 3 http://127.0.0.1:6006/api/health > /dev/null 2>&1; then
    cd /root/autodl-tmp/webapp
    nohup /root/miniconda3/bin/python app.py >> $LOG/webapp.log 2>&1 < /dev/null &
    sleep 5
    echo "星火后端已启动"
else
    echo "星火后端已在运行"
fi

# 3. 健康检查
echo "=== 健康检查 ==="
curl -s --max-time 5 http://127.0.0.1:6006/api/health
echo ""
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
