"""交互式主流程 — 启动时选择目录、显示统计、驱动处理、输出。

用法:
    python gpt-sovits-data-cleaning
    python gpt-sovits-data-cleaning run --input ./audio --speaker heroine --language ja
"""

import os
import sys
import time
from pathlib import Path

from storage.db import init_db, get_stats
from pipeline.scanner import scan_directory
from pipeline.processor import process_files
from storage.formatters import export_all

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

    # 记住目录，下次 CLI 命令自动使用
    from state import save_audio_dir
    save_audio_dir(audio_dir)

    # Step 2: 初始化数据库
    conn = init_db()

    # Step 3: 扫描目录 (insert_pending 使用 upsert，自动重置 error 为 pending)
    print(f"\n正在扫描目录...")
    scan_result = scan_directory(audio_dir, conn)

    cached = scan_result["cached"]
    new_or_changed = scan_result["new"] + scan_result["changed"]
    skipped = scan_result.get("skipped", 0)
    prev_error = get_stats(conn).get("error", 0)

    print(f"  检测到 {scan_result['total']} 个音频文件")
    print(f"    - 缓存命中 (无需处理): {cached} 个")
    if skipped > 0:
        print(f"    - 时长过滤 (跳过):    {skipped} 个")
    if new_or_changed > 0:
        parts = []
        if scan_result["new"]: parts.append(f"新增 {scan_result['new']}")
        if scan_result["changed"]: parts.append(f"变更 {scan_result['changed']}")
        if prev_error: parts.append(f"上次失败 {prev_error}")
        print(f"    - 需要处理: {', '.join(parts)}")
    else:
        print(f"    - 需要处理: 0 个")

    # 显示扫描阶段的警告 (采样率等)
    warnings = scan_result.get("warnings", [])
    if warnings:
        print(f"\n  [!] 发现 {len(warnings)} 个音频质量问题:")
        for w in warnings[:10]:
            print(f"      {w}")
        if len(warnings) > 10:
            print(f"      ... 还有 {len(warnings) - 10} 条")

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

    # Step 4: 自动修复
    fix_applied = False
    if warnings:
        print()
        do_fix = input(f"  是否自动修复这 {len(warnings)} 个问题？(重采样/归一化/去削波) [Y/n]: ").strip().lower()
        if do_fix != "n":
            print("  正在修复...")
            from audio.fixer import auto_fix
            fixed = 0
            for fp_s, status in scan_result["items"].items():
                fp = Path(fp_s)
                if fp.suffix.lower() == ".wav":
                    result = auto_fix(fp)
                    if result.get("any_fixed"):
                        fixed += 1
            print(f"  已修复 {fixed} 个文件。")
            fix_applied = True

    # Step 5: 确认
    confirm = input(f"\n是否开始处理 {new_or_changed} 个文件？[Y/n]: ").strip().lower()
    if confirm == "n":
        print("已取消。")
        conn.close()
        return

    # Step 6: 收集参数
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
        pending_files, conn, language, concurrency, fix_audio=False, on_progress=on_progress
    ))

    elapsed = time.time() - start_time
    print(f"\n\n{'─' * 40}")
    final_stats = get_stats(conn)
    print(f"ASR 完成! 耗时: {elapsed:.0f}s")
    print(f"  成功: {final_stats.get('done', 0)}  |  失败: {final_stats.get('error', 0)}")

    # 音频质量问题汇总
    quality_rows = conn.execute(
        "SELECT file_name, quality_issues FROM audio_cache WHERE quality_issues IS NOT NULL AND quality_issues != '[]' AND quality_issues != ''"
    ).fetchall()
    if quality_rows:
        print(f"\n  [!] 音频质量问题 ({len(quality_rows)} 个文件):")
        import json
        issue_counts = {}
        for r in quality_rows:
            try:
                issues = json.loads(r["quality_issues"])
            except Exception:
                issues = []
            for issue in issues:
                issue_type = issue.split(":")[0].split(" ")[0] if issue else "?"
                issue_counts[issue_type] = issue_counts.get(issue_type, 0) + 1
        for k, v in sorted(issue_counts.items(), key=lambda x: -x[1]):
            print(f"      {k}: {v} 个文件")

    print(f"{'─' * 40}")

    # Step 7: 后处理 (文本归一化 + 过滤)
    do_post = input("\n是否执行文本后处理？(日语归一化 + 语气词过滤) [Y/n]: ").strip().lower()
    if do_post != "n":
        print("\n正在后处理...")
        from pipeline.postprocess import run_postprocess
        pp_stats = run_postprocess(conn)
        print(f"  处理: {pp_stats['total']} 条")
        print(f"  文本归一化: {pp_stats['normalized']} 条")
        print(f"  过滤 (语气词/噪声): {pp_stats['filtered']} 条")
        print(f"  未变化: {pp_stats['unchanged']} 条")

        # 情感分布报告
        from storage.reports import show_emotion_distribution
        show_emotion_distribution(conn)

        # 过滤审查
        from storage.reports import show_filtered_review
        show_filtered_review(conn)
    else:
        print("已跳过后处理，ASR 原始文本将直接导出。")

    # Step 8: 导出
    do_export = input("\n是否生成输出文件？[Y/n]: ").strip().lower()
    if do_export != "n":
        print()
        _do_export_from_interactive(audio_dir, conn, speaker, language)

    conn.close()
    print("\n完成。下次选择同一目录时会自动跳过已处理的文件。")


def _do_export_from_interactive(audio_dir: Path, conn, speaker: str = "heroine", language: str = "ja"):
    output_str = input("  输出根目录 [./output]: ").strip()
    output_root = Path(output_str) if output_str else Path("./output")
    from storage.formatters import next_version
    ver = next_version(output_root)
    print(f"  输出版本: v{ver}")
    export_all(conn, audio_dir, output_root, speaker, language)


def run_cli():
    """CLI 模式入口，支持命令行参数。"""
    import argparse
    from state import load_audio_dir

    saved_dir = load_audio_dir()
    default_dir = str(saved_dir) if saved_dir else "./audio"

    parser = argparse.ArgumentParser(
        description="GPT-SoVITS 数据预处理 — 批量 ASR 转写 + 情感标注"
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="运行数据预处理")
    run_parser.add_argument("dir", nargs="?", default=default_dir,
                            help=f"音频文件目录 (默认: {default_dir})")
    run_parser.add_argument("--output", "-o", default="./output", help="输出目录")
    run_parser.add_argument("--speaker", default="heroine", help="说话人名称")
    run_parser.add_argument("--language", default="ja", help="语种代码")
    run_parser.add_argument("--concurrency", "-c", type=int, default=3, help="并发数")
    run_parser.add_argument("--skip-confirm", action="store_true", help="跳过确认提示")
    run_parser.add_argument("--fix", action="store_true", help="自动修复音频（重采样/归一化/去削波）")
    run_parser.add_argument("--skip-postprocess", action="store_true", help="跳过文本后处理（保留 ASR 原始文本）")

    clear_parser = subparsers.add_parser("clear", help="清空 SQLite 缓存，重新开始")
    clear_parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    status_parser = subparsers.add_parser("status", help="查看缓存状态")
    status_parser.add_argument("dir", nargs="?", default=default_dir, help=f"音频文件目录 (默认: {default_dir})")

    post_parser = subparsers.add_parser("postprocess", help="仅执行文本后处理（归一化+过滤），不调 ASR")
    post_parser.add_argument("dir", nargs="?", default=default_dir, help=f"音频文件目录 (默认: {default_dir})")

    fix_parser = subparsers.add_parser("fix", help="仅修复音频（重采样/归一化/去削波），不调 ASR")
    fix_parser.add_argument("dir", nargs="?", default=default_dir, help=f"音频文件目录 (默认: {default_dir})")
    fix_parser.add_argument("--dry-run", action="store_true", help="仅检测不修改")

    export_parser = subparsers.add_parser("export", help="从缓存导出标注数据")
    export_parser.add_argument("dir", nargs="?", default=default_dir, help=f"音频文件目录 (默认: {default_dir})")
    export_parser.add_argument("--output", "-o", default="./output", help="输出目录")
    export_parser.add_argument("--speaker", default="heroine", help="说话人名称")
    export_parser.add_argument("--language", default="ja", help="语种代码")

    args = parser.parse_args()

    if args.command is None:
        interactive()
        return

    if args.command == "clear":
        from storage.db import init_db, clear_all, get_stats
        conn = init_db()
        stats = get_stats(conn)
        if not stats:
            print("缓存为空，无需清空。")
            conn.close()
            return

        if not args.yes:
            print(f"将清空 {sum(stats.values())} 条记录:")
            for k, v in stats.items():
                print(f"  {k}: {v}")
            confirm = input("确认清空？[y/N]: ").strip().lower()
            if confirm != "y":
                print("已取消。")
                conn.close()
                return

        n = clear_all(conn)
        conn.close()
        print(f"已清空 {n} 条记录。下次扫描将重新处理所有文件。")
        return

    if args.command == "postprocess":
        from pipeline.postprocess import run_postprocess
        from storage.reports import show_emotion_distribution, show_filtered_review
        audio_dir = Path(args.dir)
        conn = init_db()
        stats = run_postprocess(conn)
        print(f"文本后处理完成:")
        print(f"  共处理: {stats['total']} 条")
        print(f"  文本归一化: {stats['normalized']} 条")
        print(f"  过滤: {stats['filtered']} 条")
        print(f"  未变化: {stats['unchanged']} 条")
        show_emotion_distribution(conn)
        show_filtered_review(conn)
        conn.close()
        return

    if args.command == "status":
        audio_dir = Path(args.dir)
        conn = init_db()
        scan_directory(audio_dir, conn)
        stats = get_stats(conn)
        print(f"目录:      {audio_dir}")
        print(f"文件总数:  {sum(v for v in stats.values())}")
        print(f"已完成:    {stats.get('done', 0)}")
        print(f"待处理:    {stats.get('pending', 0)}")
        print(f"失败:      {stats.get('error', 0)}")
        print(f"已过滤:    {stats.get('filtered', 0)}")

        # 音频质量问题统计
        q_rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM audio_cache WHERE quality_issues IS NOT NULL AND quality_issues != '[]' AND quality_issues != ''"
        ).fetchone()
        if q_rows and q_rows["cnt"] > 0:
            print(f"质量问题:  {q_rows['cnt']} 个文件")

        # 文本归一化统计
        n_rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM audio_cache WHERE raw_asr_text IS NOT NULL AND raw_asr_text != ''"
        ).fetchone()
        if n_rows and n_rows["cnt"] > 0:
            print(f"文本修正:  {n_rows['cnt']} 条")
        conn.close()
        return

    if args.command == "run":
        audio_dir = Path(args.dir)
        if not audio_dir.exists():
            sys.exit(f"目录不存在: {audio_dir}")

        conn = init_db()
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
            await process_files(pending_files, conn, args.language, args.concurrency, args.fix)

        asyncio.run(run())

        stats = get_stats(conn)
        print(f"ASR 完成! {time.time() - start:.0f}s | 成功: {stats.get('done', 0)} | 失败: {stats.get('error', 0)}")

        if not args.skip_postprocess:
            from pipeline.postprocess import run_postprocess
            from storage.reports import show_emotion_distribution, show_filtered_review
            pp = run_postprocess(conn)
            print(f"后处理: {pp['total']} 条, 归一化 {pp['normalized']}, 过滤 {pp['filtered']}")
            show_emotion_distribution(conn)
            show_filtered_review(conn)

        output_dir = Path(args.output)
        export_all(conn, audio_dir, output_dir, args.speaker, args.language)
        conn.close()
        return

    if args.command == "fix":
        from audio.utils import is_audio_file, check_sample_rate, detect_clipping, detect_silence
        from audio.fixer import auto_fix
        audio_dir = Path(args.dir)
        files = sorted([p for p in audio_dir.iterdir() if p.is_file() and is_audio_file(p)])
        print(f"音频文件: {len(files)} 个")
        fixed_count = 0
        for fp in files:
            if args.dry_run:
                sr = check_sample_rate(fp)
                clip = detect_clipping(fp)
                sil = detect_silence(fp)
                issues = []
                if not sr["ok"]: issues.append(sr["message"])
                if not clip["ok"]: issues.append(clip["message"])
                if not sil["ok"]: issues.append(sil["message"])
                status = "PASS" if not issues else "; ".join(issues)
                print(f"  {fp.name}: {status}")
            else:
                result = auto_fix(fp)
                if result.get("any_fixed"):
                    fixed_count += 1
                    parts = []
                    if result.get("sample_rate", {}).get("fixed"):
                        parts.append(f"重采样 {result['sample_rate']['method']}")
                    if result.get("loudness", {}).get("fixed"):
                        parts.append(f"响度 {result['loudness']['peak_before_db']}→{result['loudness']['peak_after_db']}dB")
                    if result.get("clipping", {}).get("fixed"):
                        parts.append(f"去削波 gain={result['clipping']['gain_applied']}")
                    print(f"  已修复 {fp.name}: {', '.join(parts)}")
        if not args.dry_run:
            print(f"\n修复完成: {fixed_count}/{len(files)} 个文件被修改")
        return

    if args.command == "export":
        from storage.formatters import next_version
        audio_dir = Path(args.dir)
        conn = init_db()
        output_root = Path(args.output)
        ver = next_version(output_root)
        print(f"输出版本: v{ver}")
        export_all(conn, audio_dir, output_root, args.speaker, args.language)
        conn.close()
        return
