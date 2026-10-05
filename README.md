# 星火 · AI 视频生成站（GitHub Pages 前端）

基于开源 **MiniMax H3**（33B 视频+音频联合生成模型）的中文 AI 视频生成网站前端。

## 架构

```
GitHub Pages（本仓库，静态前端）
        │  HTTPS + CORS
        ▼
AutoDL RTX 5090 服务器（后端：FastAPI + ComfyUI + MiniMax H3 INT8 量化版）
```

- 本仓库是**独立部署的前端静态站**，页面内所有生成请求通过浏览器直接发往后端生成服务
- 后端部署在 AutoDL GPU 服务器上（ComfyUI + pruned int8 量化权重约 41GB），生成 768p / 24fps / 4-15 秒**带立体声音频**的视频
- 视频生成完全运行在自有 GPU 上，**无 API 计费，免费使用**

## 使用方法

1. 打开本站（GitHub Pages 链接）
2. 首次使用点右上角 **「⚙ 服务设置」**，填入你的星火生成服务地址（AutoDL 控制台「自定义服务」里的链接），浏览器会记住
3. 输入提示词（建议描述镜头、运镜与伴随音频），选择时长与画面比例
4. 点「生成视频」，约 3-5 分钟出片，在线播放 / 下载（含音频）

## 提示词技巧（官方指南）

- 描述**分镜与运镜**：如"镜头缓慢推近"
- 描述**伴随音频**：对白、音效、背景音乐
- 支持中英等 11 种稳定语言

## 目录结构

- `index.html` — 全部前端代码（零依赖、零 CDN，CSS/JS 内联）
- `webapp/`（未包含在本仓库）— 后端源码：FastAPI + ComfyUI API 对接

## 相关链接

- 模型：<https://github.com/MiniMax-AI/MiniMax-H3>
- 模型权重：<https://huggingface.co/Comfy-Org/MiniMax-H3>

## 许可

页面内容按 MiniMax H3 Community License 要求展示 "Powered by MiniMax H3" 署名；商用需另行取得 MiniMax 商业许可。
