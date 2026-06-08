# GPT-SoVITS 数据预处理工具

批量 ASR 转写 + 情感标注，为 GPT-SoVITS 训练准备数据集。支持 CLI 交互模式和 Web UI。

**ASR 引擎**: qwen3-asr-flash (阿里云 DashScope 北京地域)，一次 API 调用同时完成转写和 7 类情感识别。

## 快速开始

```bash
# 1. 安装依赖
pip install dashscope fastapi uvicorn jinja2 python-multipart

# 2. 可选：安装 ffmpeg（非 WAV 格式音频自动修复需要）
#    Windows: winget install ffmpeg  或  https://ffmpeg.org/download.html
#    macOS:   brew install ffmpeg
#    Linux:   apt install ffmpeg

# 3. 配置 API Key
cp .env_temp .env
# 编辑 .env，填入 Key
# 获取地址: https://bailian.console.aliyun.com/?tab=api-key

# 4. 运行
python gpt-sovits-data-cleaning                        # CLI 交互模式
python server.py                                       # Web UI  → http://127.0.0.1:8765
```

## Web UI

```bash
python server.py                   # 默认 http://127.0.0.1:8765
python server.py --port 9000       # 自定义端口
```

### 功能页面

| 页面 | 功能 |
|------|------|
| **Dashboard** | 总览统计、情感分布、多目录管理（注册/移除）、缓存操作 |
| **Pipeline** | 目录选择 → 扫描 → 配置参数 → 实时进度 ASR 处理 (SSE) → 自动后处理 + 导出 |
| **Files** | 按目录/状态（done/pending/error/filtered）浏览已处理文件，查看转写文本 |
| **Export** | 选择目录生成 GPT-SoVITS 训练格式输出（list.txt + annotations.json） |

侧边栏底部提供浅色/暗色主题切换，偏好自动保存。

### 多目录管理

Dashboard 支持注册多个音频目录，每个目录独立统计。Pipeline/Files/Export 页面通过下拉菜单切换目录。

- 清除缓存时可选是否同时移除注册目录
- 目录列表持久化在 `.gsc_state`，重启后保留

## CLI 命令一览

```bash
python gpt-sovits-data-cleaning                        # 交互模式
python gpt-sovits-data-cleaning run [dir]              # CLI 一键模式
python gpt-sovits-data-cleaning status [dir]           # 查看缓存
python gpt-sovits-data-cleaning fix [dir] --dry-run    # 检测/修复音频
python gpt-sovits-data-cleaning postprocess [dir]      # 仅文本后处理
python gpt-sovits-data-cleaning export [dir]           # 导出训练数据
python gpt-sovits-data-cleaning clear                  # 清空缓存重新开始
```

`dir` 默认为已注册的第一个目录，未注册过则默认为 `./audio`。

## 处理流程

```
扫描目录 → 采样率预检 → 时长过滤(<0.5s />30s) → MD5 缓存查重
                ↓
        [可选] 发现问题 → 自动修复 (重采样/归一化/去削波)
                │   WAV: 内置 wave 处理
                │   OGG/MP3/FLAC: ffmpeg 转码 → 32kHz WAV
                ↓
        [ASR] qwen3-asr-flash 并发转写 → 原始文本落库
                ↓
        [后处理] 日语文本归一化 → 语气词过滤 → 情感分布 + 过滤审查
                ↓
        导出: output/v{N}/{emotion}/ + list.txt + annotations.json
```

### 阶段分离

- **ASR 阶段** 调 API，结果实时落库，中断不丢数据
- **后处理阶段** 纯本地操作，可随时重跑，调整阈值无需重新调 API
- Web UI Pipeline 默认自动跑后处理 + 导出；CLI 模式 `--skip-postprocess` 跳过

## 缓存机制

缓存数据库存储在 `.cache/` 目录下，不污染音频源目录。每次运行对比文件名 + MD5 哈希，按 `source_dir` 区分不同目录的数据。

| 情况 | 行为 |
|------|------|
| 文件名 + 哈希相同 | 跳过 |
| 哈希不同 | 文件变了，重新处理 |
| 新文件 | 加入队列 |
| 上次失败 | 自动重置重试 |

```bash
python gpt-sovits-data-cleaning clear    # 清空所有缓存记录
```

Web UI 与 CLI 共享同一 SQLite 缓存，可交替使用。

## 音频质量检测与修复

| 检查项 | 阈值 | 自动修复 |
|--------|------|----------|
| 采样率 | 需要 32kHz | WAV: 内置重采样 (48k/44.1k/16k → 32k) |
| | | 非 WAV: ffmpeg 转 32kHz/16bit/mono WAV |
| 削波 | ≥ 98% 最大振幅 | 降 gain 至 90% |
| 静音 | < -40dBFS 或静音帧 > 80% | 峰值归一化到 -3dBFS |

```bash
python gpt-sovits-data-cleaning fix --dry-run    # 仅检测
python gpt-sovits-data-cleaning fix              # 自动修复
python gpt-sovits-data-cleaning run --fix        # ASR 时顺带修复
```

**注意**: 非 WAV 格式（ogg/mp3/flac）的修复需要安装 ffmpeg。`--dry-run` 模式会检测但不动文件。

## 文本后处理

- **日语归一化**: 半角カナ→全角、全角数字/英字→半角、清理控制字符
- **语气词过滤**: 内置 30+ 日语常见语气词黑名单
- **非语言过滤**: 纯标点/括号/符号
- **情感分布报告**: 自动显示各情感占比，neutral > 70% 警告
- **过滤审查**: 按原因统计被过滤记录，方便审计

## 输出格式

每次导出自动递增版本号：

```
output/
├── v1/
│   ├── neutral/
│   ├── happy/
│   ├── sad/
│   ├── list.txt          # GPT-SoVITS 训练格式
│   └── annotations.json  # 完整标注
└── v2/
```

### list.txt

```
D:/datasets/voice/happy/rec001.wav|heroine|ja|今日は本当に嬉しいよ！
D:/datasets/voice/sad/rec002.wav|heroine|ja|もう、泣きそうだよ…
```

格式: `vocal_path|speaker_name|language|text`

语言代码: `zh`(中文) `ja`(日语) `en`(英语) `ko`(韩语) `yue`(粤语)

### annotations.json

```json
[{
  "file": "happy/rec001.wav",
  "text": "今日は本当に嬉しいよ！",
  "language": "ja",
  "emotion": { "audio": "happy", "final": "happy" },
  "confidence": 0.9,
  "duration_ms": 3200
}]
```

## 情感标签

qwen3-asr-flash 基于音频特征判定的 7 类情感：

| 标签 | 含义 |
|------|------|
| `neutral` | 平静/日常 |
| `happy` | 开心/愉快 |
| `sad` | 悲伤/低落 |
| `angry` | 愤怒/生气 |
| `surprised` | 惊讶 |
| `fearful` | 恐惧/紧张 |
| `disgusted` | 厌恶 |

## 环境要求

- Python >= 3.9
- dashscope >= 1.20
- 阿里云百炼 API Key（北京地域）
- ffmpeg（可选，非 WAV 格式自动修复需要）
- fastapi / uvicorn / jinja2 / python-multipart（Web UI 需要）

## 项目结构

```
gpt-sovits-data-cleaning/
├── .env_temp              # 配置模板
├── .gitignore
├── .gsc_state             # 已注册目录列表（自动生成）
├── .cache/                # SQLite 缓存（自动生成）
├── README.md
├── __main__.py            # CLI 入口
├── app.py                 # CLI 交互逻辑
├── server.py              # Web UI 服务器 (FastAPI + SSE)
├── env_loader.py          # .env 加载
├── state.py               # 目录列表持久化
│
├── templates/
│   └── index.html         # Web UI 前端
│
├── audio/                 # 音频处理
│   ├── utils.py           # 格式校验 + 质量检测 + 时长获取
│   └── fixer.py           # 重采样/归一化/去削波 (WAV + ffmpeg)
│
├── text/                  # 文本处理
│   ├── filters.py         # 语气词/噪声过滤
│   └── normalizer.py      # 日语归一化
│
├── api/                   # 外部服务
│   ├── asr.py             # qwen3-asr-flash (北京地域)
│   └── emotion.py         # DeepSeek 情感校验 (可选)
│
├── pipeline/              # 处理流水线
│   ├── scanner.py         # 扫描 + MD5 + 缓存对比
│   ├── processor.py       # 并发 ASR + 自动修复
│   └── postprocess.py     # 后处理 (归一化+过滤)
│
└── storage/               # 数据 + 输出
    ├── db.py              # SQLite 缓存 (含 source_dir 索引)
    ├── formatters.py      # list.txt + JSON 导出
    └── reports.py         # 情感分布 + 过滤审查
```
