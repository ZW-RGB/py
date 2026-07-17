# -*- coding: utf-8 -*-
"""
多源康养数据智能采集系统 —— 启动入口

用法:
    # API 模式（推荐 —— 直接调后端接口，最适合 RuoYi SPA 平台）
    python run.py --api --host http://192.168.18.143:1024 --username admin --password admin123 --no-db

    # HTML 模式（传统 Scrapy 爬虫）
    python run.py --url http://192.168.18.143:1024/... --no-db

    # Mock 模式
    python run.py --api --host http://192.168.18.143:1024 -u admin -p admin123 --mock-llm --no-db

    # 初始化数据库
    python run.py --init-db
"""
import os
import sys
import json
import argparse
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def parse_args():
    p = argparse.ArgumentParser(description="多源康养数据智能采集系统")

    # ── API 模式参数 ──
    p.add_argument("--api", action="store_true",
                   help="API 直调模式（推荐，适合若依 SPA 平台）")
    p.add_argument("--host", type=str, default="http://192.168.18.143:1024",
                   help="康养平台地址（默认 http://192.168.18.143:1024）")
    p.add_argument("-u", "--username", type=str, default="admin",
                   help="登录用户名（默认 admin）")
    p.add_argument("-p", "--password", type=str, default="admin123",
                   help="登录密码（默认 admin123）")

    # ── HTML 模式参数 ──
    p.add_argument("--url", type=str, default=None, help="目标 URL（HTML模式）")
    p.add_argument("--urls", type=str, default=None, help="URL 列表文件")
    p.add_argument("--follow", action="store_true", help="跟随模式：列表页发现详情页")
    p.add_argument("--detail-pattern", type=str, default=None, help="详情页正则")
    p.add_argument("--max-items", type=int, default=50, help="最大采集数")

    # ── 通用 ──
    p.add_argument("--init-db", action="store_true", help="仅初始化数据库表")
    p.add_argument("--schema", type=str, default="institution.json", help="Schema 文件")
    p.add_argument("--fewshot", type=str, default=None, help="Few-shot 文件")
    p.add_argument("--no-db", action="store_true", help="禁用 MySQL 存储")
    p.add_argument("--mock-llm", action="store_true", help="Mock LLM 模式（无 API 测试用）")

    return p.parse_args()


def run_api_mode(args):
    """API 直调模式：登录若依后端 → 拉取所有数据表 → LLM 解析 → 存储"""
    from kangyang.api_crawler import ApiCrawler

    crawler = ApiCrawler(
        base_url=args.host,
        username=args.username,
        password=args.password,
        use_llm=not args.mock_llm,
        schema_name=args.schema,
    )

    print(f"\n{'='*55}")
    print("  多源康养数据智能采集系统 [API 直调模式]")
    print(f"{'='*55}")
    print(f"  平台地址: {args.host}")
    print(f"  用户名  : {args.username}")
    print(f"  LLM     : {'Mock（规则提取）' if args.mock_llm else '真实 LLM（API）'}")
    print(f"  数据库  : {'禁用' if args.no_db else 'MySQL'}")
    print(f"  Schema  : {args.schema}")
    print(f"{'='*55}\n")

    if args.mock_llm:
        os.environ["LLM_MOCK"] = "1"

    results = crawler.run()

    # 汇总输出
    total_records = sum(r.collected_count for r in results)
    success_tables = sum(1 for r in results if r.success)

    print(f"\n{'='*55}")
    print(f"  采集完成！")
    print(f"  数据表: {success_tables}/{len(results)} 个成功")
    print(f"  总记录: {total_records} 条")
    print(f"{'='*55}")
    for r in results:
        status = "OK" if r.success else "FAIL"
        cols = ", ".join(r.columns[:6]) if r.columns else "无"
        print(f"  [{status}] {r.table_name}: {r.collected_count} 条 | 列: {cols}")
        if r.error_msg:
            print(f"        错误: {r.error_msg}")

    # 保存结果
    output_dir = os.path.join(PROJECT_ROOT, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "results.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(crawler.to_dict(), f, ensure_ascii=False, indent=2)

    print(f"\n  结果已保存: {output_path}")


def run_html_mode(args):
    """HTML 模式：传统 Scrapy 爬虫"""
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    if args.mock_llm:
        os.environ["LLM_MOCK"] = "1"

    start_urls = []
    if args.url:
        start_urls = [args.url]
    elif args.urls:
        with open(args.urls, "r", encoding="utf-8") as f:
            start_urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    if not start_urls:
        print("[ERROR] 请用 --url 或 --urls 指定目标 URL")
        sys.exit(1)

    fewshot_name = args.fewshot
    if not fewshot_name:
        fewshot_name = args.schema.replace(".json", "") + "_fewshot.json"

    parts = ["Mock LLM" if args.mock_llm else "真实 LLM"]
    if args.follow:
        parts.append("跟随模式")

    print(f"\n{'='*55}")
    print("  多源康养数据智能采集系统 [HTML 模式]")
    print(f"{'='*55}")
    print(f"  模式   : {' + '.join(parts)}")
    print(f"  Schema : {args.schema}")
    for i, url in enumerate(start_urls, 1):
        print(f"  [{i}] {url}")
    print(f"{'='*55}\n")

    settings = get_project_settings()
    if args.no_db:
        pipelines = dict(settings.get("ITEM_PIPELINES", {}))
        pipelines = {k: v for k, v in pipelines.items() if "MySQLPipeline" not in k}
        settings.set("ITEM_PIPELINES", pipelines)

    from kangyang.spiders.kangyang_spider import KangyangSpider
    process = CrawlerProcess(settings)
    process.crawl(KangyangSpider,
                  start_urls=start_urls,
                  schema_name=args.schema,
                  follow=args.follow,
                  detail_pattern=args.detail_pattern,
                  max_items=args.max_items)
    process.start()

    print(f"\n[DONE] 采集完成！结果: {os.path.join(PROJECT_ROOT, 'output', 'results.json')}")


def main():
    args = parse_args()

    if args.init_db:
        from kangyang.db.connection import init_db
        init_db()
        print("[OK] 数据库表结构初始化完成")
        return

    if args.api:
        run_api_mode(args)
    else:
        run_html_mode(args)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main()
