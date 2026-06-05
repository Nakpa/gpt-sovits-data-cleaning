"""交互式主流程 — 启动时选择目录、显示统计、驱动处理、输出。

用法:
    python gpt-sovits-data-cleaning
    python gpt-sovits-data-cleaning run --input ./audio --speaker heroine --language ja
"""

import os
import sys
import time
from pathlib import Path

from db import init_db, get_stats
from scanner import scan_directory
from processor import process_files
from formatters import export_all

BANNER = """
  ╔══════════════════════════════════╗
  ║  GPT-SoVITS 数据预处理 v0.1     ║
  ║  ASR: qwen3-asr-flash (DashScope)║
  ╚══════════════════════════════════╝
"""


def _prompt_input(prompt: str, default: str = "") -> str:
    value = input(f"  {prompt}").strip()
    return value if value else default


def interactive():
    """交互式启动。"""
    print(BANNER)

    # Step 1: 选择音频目录
    while True:
        print("请输入音频文件所在的目录路径:")
        dir_path = input("  > ").strip()
        if not dir_path:
            print("  路径不能为空。\n")
            continue
        audio_dir = Path(dir_path)
        if not audio_dir.exists():
            print(f"  目录不存在: {audio_dir}\n")
            continue
        if not audio_dir.is_dir():
            print(f"  这不是一个目录: {audio_dir}\n")
            continue
        break

    # Step 2: 初始化数据库
    conn = init_db(audio_dir)

    # Step 3: 扫描目录 (insert_pending 使用 upsert，自动重置 error 为 pending)
    print(f"\n正在扫描目录...")
    scan_result = scan_directory(audio_dir, conn)

    cached = scan_result["cached"]
    new_or_changed = scan_result["new"] + scan_result["changed"]
    prev_error = get_stats(conn).get("error", 0)

    print(f"  检测到 {scan_result['total']} 个音频文件")
    print(f"    - 缓存命中 (无需处理): {cached} 个")
    if new_or_changed > 0:
        parts = []
        if scan_result["new"]: parts.append(f"新增 {scan_result['new']}")
        if scan_result["changed"]: parts.append(f"变更 {scan_result['changed']}")
        if prev_error: parts.append(f"上次失败 {prev_error}")
        print(f"    - 需要处理: {', '.join(parts)}")
    else:
        print(f"    - 需要处理: 0 个")

    if new_or_changed == 0:
        print("\n所有文件已处理完毕，无需再次调用 API。")
        done_count = get_stats(conn).get("done", 0)
        if done_count > 0:
            do_export = input("\n是否重新生成输出文件？[Y/n]: ").strip().lower()
            if do_export != "n":
                print()
                _do_export_from_interactive(audio_dir, conn)
        conn.close()
        return

    # Step 4: 确认
    confirm = input(f"\n是否开始处理 {new_or_changed} 个文件？[Y/n]: ").strip().lower()
    if confirm == "n":
        print("已取消。")
        conn.close()
        return

    # Step 5: 收集参数
    speaker = _prompt_input("输入 speaker 名称 [heroine]: ", "heroine")
    language = _prompt_input("输入语种代码 [ja]: ", "ja")
    concurrency_str = _prompt_input("并发数 [3]: ", "3")
    try:
        concurrency = int(concurrency_str)
    except ValueError:
        concurrency = 3

    # 确认 API Key
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("\n[!] 未检测到 DASHSCOPE_API_KEY 环境变量。")
        api_key = input("  请输入你的 API Key (或按回车跳过): ").strip()
        if api_key:
            os.environ["DASHSCOPE_API_KEY"] = api_key
        else:
            print("  未设置 API Key，无法继续。请设置环境变量 DASHSCOPE_API_KEY 后重试。")
            conn.close()
            return

    # Step 6: 取所有 status='pending' 的文件（包括被 upsert 重置的 error）
    rows = conn.execute(
        "SELECT file_path FROM audio_cache WHERE status = 'pending'"
    ).fetchall()
    pending_files = [Path(r[0]) for r in rows]

    if not pending_files:
        print("没有文件需要处理。")
        conn.close()
        return

    print(f"\n{'─' * 40}")
    print(f"开始处理，共 {len(pending_files)} 个文件 (并发: {concurrency})")
    print(f"{'─' * 40}\n")

    start_time = time.time()

    def on_progress(done, total, current_file, status):
        pct = done / total * 100
        bar_len = 30
        filled = int(bar_len * done / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        elapsed = time.time() - start_time
        eta = (elapsed / done) * (total - done) if done > 0 else 0
        print(f"\r  [{bar}] {pct:5.1f}% ({done}/{total}) | ETA: {eta:.0f}s | {'ok' if status == 'ok' else 'err'} {current_file[:40]}",
              end="", flush=True)

    import asyncio
    asyncio.run(process_files(
        pending_files, conn, language, concurrency, on_progress
    ))

    elapsed = time.time() - start_time
    print(f"\n\n{'─' * 40}")
    final_stats = get_stats(conn)
    print(f"处理完成! 耗时: {elapsed:.0f}s")
    print(f"  成功: {final_stats.get('done', 0)}  |  失败: {final_stats.get('error', 0)}")
    print(f"{'─' * 40}")

    # Step 7: 导出
    do_export = input("\n是否生成输出文件？[Y/n]: ").strip().lower()
    if do_export != "n":
        print()
        _do_export_from_interactive(audio_dir, conn, speaker, language)

    conn.close()
    print("\n完成。下次选择同一目录时会自动跳过已处理的文件。")


def _do_export_from_interactive(audio_dir: Path, conn, speaker: str = "heroine", language: str = "ja"):
    output_str = input("  输出目录 [./output]: ").strip()
    output_dir = Path(output_str) if output_str else Path("./output")
    export_all(conn, audio_dir, output_dir, speaker, language)


def run_cli():
    """CLI 模式入口，支持命令行参数。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="GPT-SoVITS 数据预处理 — 批量 ASR 转写 + 情感标注"
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="运行数据预处理")
    run_parser.add_argument("--input", "-i", required=True, help="音频文件目录")
    run_parser.add_argument("--output", "-o", default="./output", help="输出目录")
    run_parser.add_argument("--speaker", default="heroine", help="说话人名称")
    run_parser.add_argument("--language", default="ja", help="语种代码")
    run_parser.add_argument("--concurrency", "-c", type=int, default=3, help="并发数")
    run_parser.add_argument("--skip-confirm", action="store_true", help="跳过确认提示")

    status_parser = subparsers.add_parser("status", help="查看缓存状态")
    status_parser.add_argument("--input", "-i", required=True, help="音频文件目录")

    export_parser = subparsers.add_parser("export", help="从缓存导出标注数据")
    export_parser.add_argument("--input", "-i", required=True, help="音频文件目录（含缓存数据库）")
    export_parser.add_argument("--output", "-o", default="./output", help="输出目录")
    export_parser.add_argument("--speaker", default="heroine", help="说话人名称")
    export_parser.add_argument("--language", default="ja", help="语种代码")

    args = parser.parse_args()

    if args.command is None:
        interactive()
        return

    if args.command == "status":
        audio_dir = Path(args.input)
        conn = init_db(audio_dir)
        scan_directory(audio_dir, conn)
        stats = get_stats(conn)
        print(f"目录:      {audio_dir}")
        print(f"文件总数:  {stats.get('done', 0) + stats.get('pending', 0) + stats.get('error', 0) + stats.get('processing', 0)}")
        print(f"已完成:    {stats.get('done', 0)}")
        print(f"待处理:    {stats.get('pending', 0)}")
        print(f"失败:      {stats.get('error', 0)}")
        conn.close()
        return

    if args.command == "run":
        audio_dir = Path(args.input)
        if not audio_dir.exists():
            sys.exit(f"目录不存在: {audio_dir}")

        conn = init_db(audio_dir)
        print(f"正在扫描: {audio_dir}")
        scan_result = scan_directory(audio_dir, conn)

        new_or_changed = scan_result["new"] + scan_result["changed"]
        print(f"文件: {scan_result['total']} | 缓存: {scan_result['cached']} | 需处理: {new_or_changed}")

        if new_or_changed == 0:
            print("没有需要处理的文件。")
            output_dir = Path(args.output)
            export_all(conn, audio_dir, output_dir, args.speaker, args.language)
            conn.close()
            return

        if not args.skip_confirm:
            confirm = input(f"确认处理 {new_or_changed} 个文件？[Y/n]: ").strip().lower()
            if confirm == "n":
                conn.close()
                return

        rows = conn.execute(
            "SELECT file_path FROM audio_cache WHERE status = 'pending'"
        ).fetchall()
        pending_files = [Path(r[0]) for r in rows]

        print(f"处理中 (并发: {args.concurrency})...")
        import asyncio
        start = time.time()

        async def run():
            await process_files(pending_files, conn, args.language, args.concurrency)

        asyncio.run(run())

        stats = get_stats(conn)
        print(f"完成! {time.time() - start:.0f}s | 成功: {stats.get('done', 0)} | 失败: {stats.get('error', 0)}")

        output_dir = Path(args.output)
        export_all(conn, audio_dir, output_dir, args.speaker, args.language)
        conn.close()
        return

    if args.command == "export":
        audio_dir = Path(args.input)
        conn = init_db(audio_dir)
        output_dir = Path(args.output)
        export_all(conn, audio_dir, output_dir, args.speaker, args.language)
        conn.close()
        return
