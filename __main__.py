"""python gpt-sovits-data-cleaning 入口."""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# 最先加载 .env，确保后续模块能读到环境变量
from env_loader import load_dotenv
loaded = load_dotenv()
if loaded:
    print(f"[env] 已从 .env 加载 {len(loaded)} 个配置项")

from app import interactive, run_cli

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli()
    else:
        interactive()
