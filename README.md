# GPT-SoVITS 数据预处理工具

批量 ASR 转写 + 情感标注 CLI 工具，为 GPT-SoVITS 训练准备数据集。

**ASR 引擎**: qwen3-asr-flash (阿里云 DashScope)，一次 API 调用同时完成转写和 7 类情感识别。

## 快速开始

```bash
# 1. 安装依赖
pip install dashscope

# 2. 配置 API Key
cd gpt-sovits-data-cleaning
cp .env_temp .env
# 编辑 .env，填入你的 Key

# 3. 运行
python gpt-sovits-data-cleaning
```

## 配置 (.env)

```ini
# 阿里云 DashScope — 必填
# 获取: https://bailian.console.aliyun.com/?tab=api-key
DASHSCOPE_API_KEY=sk-xxx

# DeepSeek — 可选，用于文本情感二次校验
# 获取: https://platform.deepseek.com/api_keys
DEEPSEEK_API_KEY=sk-xxx
```

启动时自动加载 `.env`，无需手动 `set/export` 环境变量。`.env` 已加入 `.gitignore`，不会被提交。

## 使用方式

### 交互模式（推荐）

```bash
python gpt-sovits-data-cleaning
```

按提示输入音频目录路径，工具自动扫描、显示缓存统计、确认后开始处理。

### 命令行模式

```bash
# 运行预处理
python gpt-sovits-data-cleaning run \
  --input ./audio \
  --speaker heroine \
  --language ja \
  --concurrency 3

# 查看缓存状态
python gpt-sovits-data-cleaning status --input ./audio

# 从已有缓存导出
python gpt-sovits-data-cleaning export \
  --input ./audio \
  --output ./output
```

## 工作原理

```
音频目录 → 扫描文件 + MD5哈希 → SQLite缓存查重
  ↓
仅新文件 → qwen3-asr-flash (ASR + 情感) → 质量过滤
  ↓
输出: list.txt + annotations.json + 按情感分文件夹
```

### 传输方式

音频通过 **base64 编码** 传入 API（data URI），不依赖 `file://` 协议，Windows/Linux/macOS 行为一致。单条音频 ≤ 10 秒时为 ~640KB raw → ~850KB base64，远低于 10MB 上限。

### 并发控制

每文件一次独立 API 请求，通过 `asyncio.Semaphore` 控制并发（默认 3），既避免触发限流，也保证了处理速度。2000 条 ≈ 33 分钟跑完。

### SQLite 缓存

缓存数据库 `preprocess_cache.db` 存放在音频目录下。每次运行对比文件名 + MD5 哈希：

| 情况 | 行为 |
|------|------|
| 文件名相同、哈希相同 | 跳过，不调 API |
| 文件名相同、哈希不同 | 文件变了，重新处理 |
| 新文件名 | 加入处理队列 |
| 上次处理失败 (error) | 自动重置为 pending，重新处理 |

### 质量过滤

ASR 完成后自动过滤无效音频（标记为 `filtered`，不导出到训练数据）：

- 空白文本
- 纯非语言符号（`（笑）` `*注釈*` 等标注）
- 不含任何假名/汉字的文本
- 去除标点后不足 2 个有效字符
- 纯语气词（`あっ` `うん` `えっと` `はい` `まあ` 等 30+ 常见词）

## 输出格式

```
output/
├── neutral/          # 按情感分类的音频
│   ├── rec001.wav
│   └── rec005.wav
├── happy/
│   └── rec002.wav
├── sad/
│   └── rec003.wav
├── list.txt          # GPT-SoVITS 训练格式
└── annotations.json  # 完整标注数据
```

### list.txt

GPT-SoVITS 原生训练格式：

```
happy/rec001.wav|heroine|ja|happy|今日は本当に嬉しいよ！
sad/rec002.wav|heroine|ja|sad|もう、泣きそうだよ…
```

格式: `相对路径|speaker|language|emotion|text`

### annotations.json

完整结构化标注：

```json
[
  {
    "file": "happy/rec001.wav",
    "text": "今日は本当に嬉しいよ！",
    "language": "ja",
    "emotion": {
      "audio": "happy",
      "text_semantic": null,
      "final": "happy"
    },
    "confidence": 0.9,
    "duration_ms": 3200,
    "file_hash": "7d3fadf...",
    "processed_at": "2026-06-05 08:03:34"
  }
]
```

## 情感标签

qwen3-asr-flash 原生支持 7 类情感，基于音频特征（语调/语速/音高）判定：

| 标签 | 含义 |
|------|------|
| `neutral` | 平静/日常 |
| `happy` | 开心/愉快 |
| `sad` | 悲伤/低落 |
| `angry` | 愤怒/生气 |
| `surprised` | 惊讶 |
| `fearful` | 恐惧/紧张 |
| `disgusted` | 厌恶 |

如需文本语义层面的情感二次校验，可启用 DeepSeek 模块（`emotion_deepseek.py`）。

## 环境要求

- Python >= 3.9
- dashscope >= 1.20 (阿里云 DashScope SDK)
- 阿里云百炼 API Key ([获取地址](https://bailian.console.aliyun.com/?tab=api-key))

## 文件结构

```
gpt-sovits-data-cleaning/
├── .env_temp            # 配置模板
├── .gitignore
├── README.md
├── __main__.py          # 入口
├── app.py               # 交互式 CLI
├── asr_qwen.py          # qwen3-asr-flash API
├── audio_utils.py       # 音频校验
├── db.py                # SQLite 缓存
├── env_loader.py        # .env 加载
├── filters.py           # 文本质量过滤
├── formatters.py        # 输出格式化
├── processor.py         # 并发处理器
├── scanner.py           # 目录扫描 + MD5
└── emotion_deepseek.py  # DeepSeek 情感校验 (可选)
```
