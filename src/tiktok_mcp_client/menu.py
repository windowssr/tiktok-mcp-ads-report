from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tiktok_mcp_client.cli import (
    DEFAULT_CALLBACK_PORT,
    DEFAULT_SERVER_URL,
    DEFAULT_STATE_DIR,
    TikTokMcpClient,
    build_basic_report_arguments,
    build_client,
    build_creative_report_params,
    call_once,
    command_auth,
    command_logout,
    fetch_all_advertiser_creative_reports,
    fetch_all_advertiser_reports,
    parse_advertisers,
    render_dynamic_values,
    save_rows,
)
from tiktok_mcp_client.report import (
    default_output_stem,
    resolve_date_preset,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data"


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true"}


def pause() -> None:
    input("\n按回车返回菜单...")


def connection_namespace(
    *,
    proxy: str | None,
    server_url: str,
    token_file: str,
    callback_port: int,
    no_browser: bool,
    debug: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        proxy=proxy,
        server_url=server_url,
        token_file=token_file,
        callback_port=callback_port,
        no_browser=no_browser,
        debug=debug,
    )


def open_path(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        print(f"路径不存在：{path}")
        return
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def choose_preset() -> tuple[str, str | None, str | None, bool]:
    print(
        """
时间范围:
  1) 今天
  2) 昨天
  3) 近 7 天
  4) 近 14 天
  5) 近 30 天
  6) 本月至今
  7) 自定义起止日期
  8) 全量 lifetime（不限日期）
""".rstrip()
    )
    choice = ask("请选择", "3")
    mapping = {
        "1": "today",
        "2": "yesterday",
        "3": "last_7_days",
        "4": "last_14_days",
        "5": "last_30_days",
        "6": "this_month",
        "7": "custom",
        "8": "lifetime",
    }
    preset = mapping.get(choice, "last_7_days")
    if preset == "custom":
        start_date = ask("开始日期 YYYY-MM-DD")
        end_date = ask("结束日期 YYYY-MM-DD")
        return preset, start_date, end_date, False
    start_token, end_token, lifetime = resolve_date_preset(preset)
    return preset, start_token, end_token, lifetime


def choose_report_kind() -> tuple[str, str]:
    """Return (mode, data_level). mode is basic|material."""
    print(
        """
报表类型（投流常用）:
  1) 计划 Campaign
  2) 广告组 Ad Group
  3) 广告 Ad（广告名 + 视频完播等，看单条广告消耗）
  4) 素材 Material（按视频素材汇总消耗，看素材消耗）
  5) 账户 Advertiser
""".rstrip()
    )
    choice = ask("请选择", "4")
    mapping = {
        "1": ("basic", "AUCTION_CAMPAIGN"),
        "2": ("basic", "AUCTION_ADGROUP"),
        "3": ("basic", "AUCTION_AD"),
        "4": ("material", "AUCTION_AD"),
        "5": ("basic", "AUCTION_ADVERTISER"),
    }
    return mapping.get(choice, ("material", "AUCTION_AD"))


def choose_formats() -> list[str]:
    print(
        """
导出格式（可多选，逗号分隔）:
  1) Excel (.xlsx)  推荐
  2) CSV
  3) JSON
""".rstrip()
    )
    raw = ask("请选择", "1,2")
    mapping = {"1": "xlsx", "2": "csv", "3": "json"}
    selected: list[str] = []
    for part in raw.replace("，", ",").split(","):
        item = mapping.get(part.strip())
        if item and item not in selected:
            selected.append(item)
    return selected or ["xlsx", "csv"]


async def list_advertisers(client: TikTokMcpClient) -> list[dict[str, str]]:
    result = await call_once(client, "auth_advertiser_get", {})
    advertisers = parse_advertisers(result)
    print(f"\n共 {len(advertisers)} 个授权广告账户：\n")
    for index, item in enumerate(advertisers, start=1):
        print(
            f"{index:>3}. {item['advertiser_name'] or '(无名称)'}  "
            f"{item['advertiser_id']}"
        )
    return advertisers


async def run_fetch(
    ns: SimpleNamespace,
    *,
    preset: str,
    start_date: str | None,
    end_date: str | None,
    query_lifetime: bool,
    mode: str,
    data_level: str,
    formats: list[str],
    advertiser_ids: list[str] | None,
    advertiser_keyword: str | None,
    only_active: bool,
    only_spend: bool,
    output_dir: Path,
) -> list[Path]:
    if mode == "material":
        creative_params = build_creative_report_params(
            material_type="VIDEO",
            start_date=start_date,
            end_date=end_date,
            query_lifetime=query_lifetime,
        )
        if query_lifetime:
            resolved_start = None
            resolved_end = None
        else:
            creative_params = render_dynamic_values(creative_params)
            resolved_start = creative_params.get("start_date")
            resolved_end = creative_params.get("end_date")

        print("\n开始拉取素材消耗...")
        print(
            f"  时间: "
            f"{'lifetime' if query_lifetime else f'{resolved_start} ~ {resolved_end}'}"
        )
        print("  粒度: 素材 Material (creative_report_get / VIDEO)")
        print(f"  格式: {', '.join(formats)}")

        payload = await fetch_all_advertiser_creative_reports(
            build_client(ns),
            creative_params,
            material_types=["VIDEO"],
            advertiser_ids=advertiser_ids,
            advertiser_keyword=advertiser_keyword,
            only_active_rows=only_active,
            only_spend_rows=only_spend,
            max_retries=2,
        )
        level_tag = "material"
    else:
        report_arguments = build_basic_report_arguments(
            data_level=data_level,
            start_date=start_date,
            end_date=end_date,
            query_lifetime=query_lifetime,
        )
        if query_lifetime:
            resolved_start = None
            resolved_end = None
        else:
            report_arguments = render_dynamic_values(report_arguments)
            resolved_start = report_arguments.get("start_date")
            resolved_end = report_arguments.get("end_date")

        print("\n开始拉取...")
        print(
            f"  时间: "
            f"{'lifetime' if query_lifetime else f'{resolved_start} ~ {resolved_end}'}"
        )
        print(
            f"  粒度: {data_level} / dimensions="
            f"{report_arguments.get('dimensions')}"
        )
        print(f"  格式: {', '.join(formats)}")

        payload = await fetch_all_advertiser_reports(
            build_client(ns),
            report_arguments,
            advertiser_ids=advertiser_ids,
            advertiser_keyword=advertiser_keyword,
            only_active_rows=only_active,
            only_spend_rows=only_spend,
            max_retries=2,
        )
        level_tag = data_level.lower()

    label = default_output_stem(
        preset=preset,
        start_date=str(resolved_start) if resolved_start else None,
        end_date=str(resolved_end) if resolved_end else None,
        query_lifetime=query_lifetime,
    )
    label = f"{label}_{level_tag}"
    paths = save_rows(output_dir, label, payload, payload["rows"], formats)
    totals = (payload.get("summary") or {}).get("totals") or {}
    print("\n拉取完成")
    print(f"  账户数: {payload['advertiser_count']}")
    print(f"  明细行: {payload['row_count']}")
    print(f"  错误数: {payload['error_count']}")
    print(f"  总消耗: {totals.get('spend')}")
    print(f"  点击: {totals.get('clicks')}")
    print(f"  转化: {totals.get('conversion')}")
    print("  输出文件:")
    for path in paths:
        print(f"    - {path.resolve()}")
    return paths


async def interactive_loop(ns: SimpleNamespace, output_dir: Path) -> int:
    while True:
        print(
            f"""
========== TikTok 投流数据采集 ==========
代理: {ns.proxy or '(未设置)'}
输出目录: {output_dir.resolve()}
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

  1) 授权 / 检查登录
  2) 查看授权广告账户
  3) 按时间拉取（全账户）
  4) 全量 lifetime 拉取
  5) 按账户关键字拉取
  6) 指定账户 ID 拉取
  7) 打开输出目录
  8) 修改代理端口
  9) 退出登录（删除本地 Token）
  0) 退出
========================================
""".rstrip()
        )
        choice = ask("请选择", "3")
        try:
            if choice == "0":
                print("已退出。")
                return 0
            if choice == "1":
                await command_auth(ns)
                pause()
            elif choice == "2":
                await list_advertisers(build_client(ns))
                pause()
            elif choice in {"3", "4", "5", "6"}:
                if choice == "4":
                    preset, start_date, end_date, lifetime = (
                        "lifetime",
                        None,
                        None,
                        True,
                    )
                else:
                    preset, start_date, end_date, lifetime = choose_preset()
                mode, data_level = choose_report_kind()
                formats = choose_formats()
                only_spend = ask_yes_no("只保留有消耗的行？", True)
                only_active = False if only_spend else ask_yes_no(
                    "只保留有曝光/点击/转化的行？", True
                )
                advertiser_ids = None
                advertiser_keyword = None
                if choice == "5":
                    advertiser_keyword = ask("账户名关键字")
                if choice == "6":
                    raw_ids = ask("账户 ID（多个用逗号分隔）")
                    advertiser_ids = [
                        item.strip()
                        for item in raw_ids.split(",")
                        if item.strip()
                    ]
                paths = await run_fetch(
                    ns,
                    preset=preset,
                    start_date=start_date,
                    end_date=end_date,
                    query_lifetime=lifetime,
                    mode=mode,
                    data_level=data_level,
                    formats=formats,
                    advertiser_ids=advertiser_ids,
                    advertiser_keyword=advertiser_keyword,
                    only_active=only_active,
                    only_spend=only_spend,
                    output_dir=output_dir,
                )
                if paths and ask_yes_no("是否打开输出目录？", True):
                    open_path(output_dir)
                pause()
            elif choice == "7":
                output_dir.mkdir(parents=True, exist_ok=True)
                open_path(output_dir)
            elif choice == "8":
                port = ask("HTTP 代理端口", "7890")
                ns.proxy = f"http://127.0.0.1:{port}"
                print(f"已更新代理：{ns.proxy}")
            elif choice == "9":
                await command_logout(ns)
                pause()
            else:
                print("无效选项。")
        except KeyboardInterrupt:
            print("\n已取消当前操作。")
        except Exception as error:
            print(f"\n错误：{error}")
            if ns.debug:
                raise
            pause()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TikTok 投流数据交互采集脚本（支持按时间/全量/Excel）"
    )
    parser.add_argument(
        "--proxy",
        default=os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or "http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--server-url",
        default=os.getenv("TIKTOK_MCP_URL", DEFAULT_SERVER_URL),
    )
    parser.add_argument(
        "--token-file",
        default=os.getenv(
            "TIKTOK_MCP_TOKEN_FILE",
            str(DEFAULT_STATE_DIR / "oauth.json"),
        ),
    )
    parser.add_argument(
        "--callback-port",
        type=int,
        default=int(
            os.getenv("TIKTOK_MCP_CALLBACK_PORT", DEFAULT_CALLBACK_PORT)
        ),
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
    )
    parser.add_argument(
        "--once",
        choices=[
            "today",
            "yesterday",
            "last_7_days",
            "last_14_days",
            "last_30_days",
            "this_month",
            "lifetime",
        ],
        help="非交互：直接按预设拉取一次并退出",
    )
    parser.add_argument("--start-date", help="配合 --once custom 使用")
    parser.add_argument("--end-date", help="配合 --once custom 使用")
    parser.add_argument(
        "--format",
        action="append",
        choices=["json", "csv", "xlsx", "excel"],
        default=None,
    )
    parser.add_argument("--only-spend", action="store_true")
    parser.add_argument("--only-active", action="store_true")
    parser.add_argument(
        "--mode",
        default="material",
        choices=["basic", "material"],
        help="once 模式默认拉素材消耗；basic 配合 --data-level",
    )
    parser.add_argument(
        "--data-level",
        default="AUCTION_AD",
        choices=[
            "AUCTION_CAMPAIGN",
            "AUCTION_ADGROUP",
            "AUCTION_AD",
            "AUCTION_ADVERTISER",
        ],
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ns = connection_namespace(
        proxy=args.proxy,
        server_url=args.server_url,
        token_file=args.token_file,
        callback_port=args.callback_port,
        no_browser=args.no_browser,
        debug=args.debug,
    )
    output_dir = Path(args.output_dir)

    async def _run() -> int:
        if args.once:
            preset = args.once
            start_date, end_date, lifetime = resolve_date_preset(
                preset,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            formats = args.format or ["xlsx", "csv", "json"]
            await run_fetch(
                ns,
                preset=preset,
                start_date=start_date,
                end_date=end_date,
                query_lifetime=lifetime,
                mode=args.mode,
                data_level=args.data_level,
                formats=formats,
                advertiser_ids=None,
                advertiser_keyword=None,
                only_active=args.only_active,
                only_spend=args.only_spend or not args.only_active,
                output_dir=output_dir,
            )
            return 0
        return await interactive_loop(ns, output_dir)

    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        print("\n已退出。")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
