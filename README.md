# GPT-SoVITS 数据预处理工具

批量 ASR 转写 + 情感标注 CLI 工具，为 GPT-SoVITS 训练准备数据集。

**ASR 引擎**: qwen3-asr-flash (阿里云 DashScope 北京地域)，一次 API 调用同时完成转写和 7 类情感识别。

## 快速开始

```bash
# 1. 安装依赖
pip install dashscope

# 2. 配置 API Key
cp .env_temp .env
# 编辑 .env，填入 Key
# 获取地址: https://bailian.console.aliyun.com/?tab=api-key

# 3. 运行（交互模式）
python gpt-sovits-data-cleaning
```

## 配置 (.env)

```ini
DASHSCOPE_API_KEY=sk-xxx     # 必填
DEEPSEEK_API_KEY=sk-xxx      # 可选，文本情感二次校验
```

启动时自动加载，无需手动 `set/export`。

## 命令一览

```bash
python gpt-sovits-data-cleaning                        # 交互模式
python gpt-sovits-data-cleaning run [dir]              # CLI 模式
python gpt-sovits-data-cleaning status [dir]           # 查看缓存
python gpt-sovits-data-cleaning fix [dir] --dry-run    # 检测/修复音频
python gpt-sovits-data-cleaning postprocess [dir]      # 仅文本后处理
python gpt-sovits-data-cleaning export [dir]           # 导出训练数据
python gpt-sovits-data-cleaning clear                  # 清空缓存重新开始
```

`dir` 默认为交互模式上次选择的目录；未使用过交互模式则默认为 `./audio`。

## 处理流程

```
扫描目录 → 采样率预检 → 时长过滤(<0.5s />30s) → MD5 缓存查重
                ↓
        [可选] 发现问题 → 自动修复(重采样/归一化/去削波)
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
- CLI 模式默认自动跑后处理，`--skip-postprocess` 跳过

## 缓存机制

缓存数据库存储在工具自身的 `.cache/` 目录下，不污染音频源目录。每次运行对比文件名 + MD5 哈希：

| 情况 | 行为 |
|------|------|
| 文件名 + 哈希相同 | 跳过 |
| 哈希不同 | 文件变了，重新处理 |
| 新文件 | 加入队列 |
| 上次失败 | 自动重置重试 |

交互模式选过的目录会记在 `.gsc_state`，后续 CLI 命令自动读取。

```bash
python gpt-sovits-data-cleaning clear    # 清空所有缓存记录
```

## 音频质量检测与修复

| 检查项 | 阈值 | 自动修复 |
|--------|------|----------|
| 采样率 | 需要 32kHz | 重采样 (48k/44.1k/16k → 32k) |
| 削波 | ≥ 98% 最大振幅 | 降 gain 至 90% |
| 静音 | < -40dBFS 或静音帧 > 80% | 峰值归一化到 -3dBFS |

```bash
python gpt-sovits-data-cleaning fix --dry-run    # 仅检测
python gpt-sovits-data-cleaning fix              # 自动修复
python gpt-sovits-data-cleaning run --fix        # ASR 时顺带修复
```

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
happy/rec001.wav|heroine|ja|happy|今日は本当に嬉しいよ！
sad/rec002.wav|heroine|ja|sad|もう、泣きそうだよ…
```

格式: `相对路径|speaker|language|emotion|text`

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

## 项目结构

```
gpt-sovits-data-cleaning/
├── .env_temp              # 配置模板
├── .gitignore
├── .gsc_state             # 记住上次的音频目录（自动生成）
├── .cache/                # SQLite 缓存（自动生成）
├── README.md
├── __main__.py            # 入口
├── app.py                 # CLI 交互
├── env_loader.py          # .env 加载
├── state.py               # 状态持久化
│
├── audio/                 # 音频处理
│   ├── utils.py           # 格式校验 + 质量检测
│   └── fixer.py           # 重采样/归一化/去削波
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
│   ├── processor.py       # 并发 ASR
│   └── postprocess.py     # 后处理 (归一化+过滤)
│
└── storage/               # 数据 + 输出
    ├── db.py              # SQLite 缓存
    ├── formatters.py      # list.txt + JSON 导出
    └── reports.py         # 情感分布 + 过滤审查
```
