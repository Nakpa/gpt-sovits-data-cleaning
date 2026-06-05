""".env 文件加载器 — 零依赖，不引入 python-dotenv。

加载优先级: .env 文件 > 系统环境变量 (系统环境变量会覆盖 .env 的值)
"""

import os
from pathlib import Path


def load_dotenv(env_path: Path = None) -> dict[str, str]:
    """加载 .env 文件到 os.environ，返回加载的键值对。

    不覆盖已存在的系统环境变量（系统变量优先）。
    支持: KEY=VALUE, KEY="VALUE", KEY='VALUE', # 注释, 空行
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"

    if not env_path.exists():
        return {}

    loaded = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue

            # 解析 KEY=VALUE
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()

            # 去掉引号
            value = value.strip()
            if len(value) >= 2:
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

            # 已存在的系统环境变量不覆盖
            if key in os.environ:
                continue

            os.environ[key] = value
            loaded[key] = value

    return loaded
