from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from tiktok_mcp_client.report import (
    build_summary,
    default_output_stem,
    filter_rows_with_activity,
    resolve_date_preset,
    write_excel,
)


DEFAULT_SERVER_URL = (
    "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-layer"
)
DEFAULT_CALLBACK_PORT = 33418
DEFAULT_STATE_DIR = Path.home() / ".tiktok_official_mcp"


class FileTokenStorage(TokenStorage):
    """Persist OAuth client registration and tokens for unattended later runs."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = asyncio.Lock()

    async def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(await asyncio.to_thread(self.path.read_text, "utf-8"))

    async def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        text = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(temporary.write_text, text, "utf-8")
        await asyncio.to_thread(os.replace, temporary, self.path)
        try:
            await asyncio.to_thread(os.chmod, self.path, 0o600)
        except OSError:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            raw = (await self._read()).get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            data = await self._read()
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            await self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            raw = (await self._read()).get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        async with self._lock:
            data = await self._read()
            data["client_info"] = client_info.model_dump(
                mode="json", exclude_none=True
            )
            await self._write(data)

    async def clear(self) -> None:
        async with self._lock:
            if self.path.exists():
                await asyncio.to_thread(self.path.unlink)


class OAuthCallbackReceiver:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.redirect_url = f"http://{host}:{port}/callback"
        self._results: queue.Queue[tuple[str, str | None] | Exception] = (
            queue.Queue(maxsize=1)
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        results = self._results

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                params = parse_qs(urlparse(self.path).query)
                if "error" in params:
                    message = params.get("error_description", params["error"])[0]
                    result: tuple[str, str | None] | Exception = RuntimeError(
                        f"OAuth authorization failed: {message}"
                    )
                    status = 400
                    body = "TikTok authorization failed. You may close this window."
                elif "code" not in params:
                    result = RuntimeError("OAuth callback did not contain a code")
                    status = 400
                    body = "Missing authorization code. You may close this window."
                else:
                    result = (
                        params["code"][0],
                        params.get("state", [None])[0],
                    )
                    status = 200
                    body = (
                        "TikTok authorization succeeded. "
                        "You may close this window and return to the terminal."
                    )
                try:
                    results.put_nowait(result)
                except queue.Full:
                    pass
                encoded = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tiktok-mcp-oauth-callback",
            daemon=True,
        )
        self._thread.start()

    async def wait(self, timeout: int = 300) -> tuple[str, str | None]:
        try:
            result = await asyncio.to_thread(
                self._results.get, True, timeout
            )
        except queue.Empty as error:
            raise TimeoutError(
                f"OAuth callback was not received within {timeout} seconds"
            ) from error
        if isinstance(result, Exception):
            raise result
        return result

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)


class TikTokMcpClient:
    def __init__(
        self,
        server_url: str,
        token_file: Path,
        proxy: str | None,
        callback_port: int,
        no_browser: bool = False,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.storage = FileTokenStorage(token_file)
        self.proxy = proxy
        self.callback = OAuthCallbackReceiver(
            "127.0.0.1", callback_port
        )
        self.no_browser = no_browser

    async def _redirect(self, authorization_url: str) -> None:
        print(f"\n请在浏览器完成 TikTok 授权：\n{authorization_url}\n")
        if not self.no_browser:
            await asyncio.to_thread(webbrowser.open, authorization_url)

    async def _callback(self) -> tuple[str, str | None]:
        return await self.callback.wait()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        try:
            self.callback.start()
        except OSError as error:
            raise RuntimeError(
                f"无法监听 OAuth 回调端口 {self.callback.port}，"
                "请使用 --callback-port 更换端口"
            ) from error

        oauth = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name="TikTok Official MCP Automation Client",
                redirect_uris=[AnyUrl(self.callback.redirect_url)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="mcp:tt4b",
            ),
            storage=self.storage,
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )
        timeout = httpx.Timeout(60.0, read=300.0)
        try:
            async with httpx.AsyncClient(
                auth=oauth,
                follow_redirects=True,
                timeout=timeout,
                proxy=self.proxy,
                trust_env=True,
            ) as http_client:
                transport = streamable_http_client(
                    self.server_url, http_client=http_client
                )
                async with transport as streams:
                    read_stream, write_stream = streams[0], streams[1]
                    async with ClientSession(
                        read_stream, write_stream
                    ) as session:
                        await session.initialize()
                        yield session
        finally:
            self.callback.stop()


def parse_json_argument(value: str) -> dict[str, Any]:
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8-sig")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是一个 JSON 对象")
    return parsed


def tool_to_dict(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        return tool.model_dump(mode="json", exclude_none=True)
    return {
        "name": tool.name,
        "description": getattr(tool, "description", None),
        "inputSchema": getattr(tool, "inputSchema", {}),
    }


def result_to_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        raw = result.model_dump(mode="json", exclude_none=True)
    else:
        raw = {"content": str(result)}

    parsed_text: list[Any] = []
    for content in raw.get("content", []):
        if not isinstance(content, dict) or "text" not in content:
            continue
        try:
            parsed_text.append(json.loads(content["text"]))
        except (json.JSONDecodeError, TypeError):
            continue
    if len(parsed_text) == 1:
        raw["parsed"] = parsed_text[0]
    elif parsed_text:
        raw["parsed"] = parsed_text
    return raw


DATA_LEVEL_DIMENSIONS: dict[str, list[str]] = {
    "AUCTION_CAMPAIGN": ["campaign_id"],
    "AUCTION_ADGROUP": ["adgroup_id"],
    "AUCTION_AD": ["ad_id"],
    "AUCTION_ADVERTISER": ["advertiser_id"],
}

# 投流默认指标：消耗拆分 + 互动 + Native Growth D0~D31 收益/ROAS
# advertiser_name / advertiser_id 由客户端按账户附加，无需放进 metrics
CORE_PERF_METRICS = [
    "spend",
    "cash_spend",
    "voucher_spend",
    "cpc",
    "cpm",
    "impressions",
    "conversion",
    "cost_per_conversion",
    "engagements",
    "engagement_rate",
    "native_growth_ad_revenue_value_d0",
    "native_growth_ad_revenue_value_d1",
    "native_growth_ad_revenue_value_d2",
    "native_growth_ad_revenue_value_d3",
    "native_growth_ad_revenue_value_d4",
    "native_growth_ad_revenue_value_d5",
    "native_growth_ad_revenue_value_d6",
    "native_growth_ad_revenue_value_d13",
    "native_growth_ad_revenue_value_d20",
    "native_growth_ad_revenue_value_d27",
    "native_growth_ad_revenue_value_d29",
    "native_growth_ad_revenue_value_d31",
    "native_growth_ad_revenue_roas_d0",
    "native_growth_ad_revenue_roas_d1",
    "native_growth_ad_revenue_roas_d2",
    "native_growth_ad_revenue_roas_d3",
    "native_growth_ad_revenue_roas_d4",
    "native_growth_ad_revenue_roas_d5",
    "native_growth_ad_revenue_roas_d6",
    "native_growth_ad_revenue_roas_d13",
    "native_growth_ad_revenue_roas_d20",
    "native_growth_ad_revenue_roas_d27",
    "native_growth_ad_revenue_roas_d29",
    "native_growth_ad_revenue_roas_d31",
]

VIDEO_PERF_METRICS = [
    "video_play_actions",
    "video_watched_2s",
    "video_watched_6s",
    "average_video_play",
    "video_views_p25",
    "video_views_p50",
    "video_views_p75",
    "video_views_p100",
]

DEFAULT_ROAS_METRICS = ["campaign_name", *CORE_PERF_METRICS]

DEFAULT_ADGROUP_METRICS = [
    "adgroup_name",
    "campaign_name",
    *CORE_PERF_METRICS,
]

DEFAULT_AD_METRICS = [
    "ad_name",
    "adgroup_name",
    "campaign_name",
    *CORE_PERF_METRICS,
    *VIDEO_PERF_METRICS,
]

CREATIVE_INFO_FIELDS = [
    "material_id",
    "material_name",
    "video_id",
    "image_id",
    "video_material_source",
    "identity",
    "placement",
    "country_code",
    "currency",
    "create_time",
    "tiktok_item_ids",
]

# creative_report_get 仅支持以下指标；现金/赠款/Native Growth 需走广告级报表
CREATIVE_METRICS_FIELDS = [
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "conversion",
    "cost_per_conversion",
    *VIDEO_PERF_METRICS,
]


def dimensions_for_data_level(data_level: str) -> list[str]:
    return list(
        DATA_LEVEL_DIMENSIONS.get(data_level, ["campaign_id"])
    )


def metrics_for_data_level(data_level: str) -> list[str]:
    if data_level == "AUCTION_AD":
        return list(DEFAULT_AD_METRICS)
    if data_level == "AUCTION_ADGROUP":
        return list(DEFAULT_ADGROUP_METRICS)
    if data_level == "AUCTION_ADVERTISER":
        return list(CORE_PERF_METRICS)
    return list(DEFAULT_ROAS_METRICS)


def build_basic_report_arguments(
    *,
    data_level: str,
    start_date: str | None = None,
    end_date: str | None = None,
    query_lifetime: bool = False,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "report_type": "BASIC",
        "data_level": data_level,
        "dimensions": dimensions_for_data_level(data_level),
        "metrics": metrics_for_data_level(data_level),
        "page_size": 1000,
        "order_field": "spend",
        "order_type": "DESC",
    }
    if query_lifetime:
        arguments["query_lifetime"] = True
    else:
        arguments["start_date"] = start_date or "${week_ago}"
        arguments["end_date"] = end_date or "${today}"
    return arguments


def build_creative_report_params(
    *,
    material_type: str = "VIDEO",
    start_date: str | None = None,
    end_date: str | None = None,
    query_lifetime: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "report_type": "VIDEO_INSIGHT",
        "material_type": material_type,
        "page_size": 1000,
        "sort_field": "spend",
        "sort_type": "DESC",
        "info_fields": list(CREATIVE_INFO_FIELDS),
        "metrics_fields": list(CREATIVE_METRICS_FIELDS),
    }
    if query_lifetime:
        params["lifetime"] = True
    else:
        params["start_date"] = start_date or "${week_ago}"
        params["end_date"] = end_date or "${today}"
        params["lifetime"] = False
    return params


def render_dynamic_values(value: Any, now: datetime | None = None) -> Any:
    now = now or datetime.now()
    today = now.date()
    replacements = {
        "${today}": today.isoformat(),
        "${yesterday}": (today - timedelta(days=1)).isoformat(),
        "${week_ago}": (today - timedelta(days=7)).isoformat(),
        "${days_ago_14}": (today - timedelta(days=14)).isoformat(),
        "${days_ago_30}": (today - timedelta(days=30)).isoformat(),
        "${month_start}": today.replace(day=1).isoformat(),
        "${now}": now.isoformat(timespec="seconds"),
    }
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [render_dynamic_values(item, now) for item in value]
    if isinstance(value, dict):
        return {
            key: render_dynamic_values(item, now)
            for key, item in value.items()
        }
    return value


def extract_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(
        isinstance(item, dict) for item in value
    ):
        return value
    if not isinstance(value, dict):
        return []
    for key in (
        "parsed",
        "structuredContent",
        "structured_content",
        "list",
        "rows",
        "items",
        "results",
    ):
        rows = extract_rows(value.get(key))
        if rows:
            return rows
    data = value.get("data")
    if data is not None:
        rows = extract_rows(data)
        if rows:
            return rows
    return []


def flatten_dict(
    value: dict[str, Any], prefix: str = ""
) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flattened.update(flatten_dict(item, full_key))
        elif isinstance(item, list):
            flattened[full_key] = json.dumps(
                item, ensure_ascii=False, separators=(",", ":")
            )
        else:
            flattened[full_key] = item
    return flattened


def save_rows(
    output_dir: Path,
    label: str,
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    formats: list[str],
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in label
    )
    if safe_label[:8].isdigit() and "_" in safe_label:
        base = output_dir / safe_label
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = output_dir / f"{timestamp}_{safe_label}"
    written: list[Path] = []

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = build_summary(rows)
        payload = {**payload, "summary": summary}

    if "json" in formats:
        path = base.with_suffix(".json")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(path)

    if "csv" in formats and rows:
        flat_rows = [flatten_dict(row) for row in rows]
        fieldnames = list(
            dict.fromkeys(key for row in flat_rows for key in row)
        )
        path = base.with_suffix(".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_rows)
        written.append(path)

    if "xlsx" in formats or "excel" in formats:
        path = write_excel(
            base.with_suffix(".xlsx"),
            detail_rows=rows,
            summary=summary,
            account_summaries=payload.get("account_summaries") or [],
            errors=payload.get("errors") or [],
            meta={
                "fetched_at": payload.get("fetched_at"),
                "advertiser_count": payload.get("advertiser_count"),
                "row_count": payload.get("row_count"),
                "error_count": payload.get("error_count"),
                "label": safe_label,
            },
            flatten_row=flatten_dict,
        )
        written.append(path)

    return written


def save_result(
    output_dir: Path,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    formats: list[str],
) -> list[Path]:
    return save_rows(
        output_dir,
        tool_name,
        {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "arguments": arguments,
            "result": result,
        },
        extract_rows(result),
        formats,
    )


def build_client(args: argparse.Namespace) -> TikTokMcpClient:
    return TikTokMcpClient(
        server_url=args.server_url,
        token_file=Path(args.token_file),
        proxy=args.proxy,
        callback_port=args.callback_port,
        no_browser=args.no_browser,
    )


async def command_auth(args: argparse.Namespace) -> int:
    client = build_client(args)
    async with client.session() as session:
        tools = await session.list_tools()
    print(f"授权及连接成功，可用工具数：{len(tools.tools)}")
    return 0


async def command_tools(args: argparse.Namespace) -> int:
    client = build_client(args)
    async with client.session() as session:
        response = await session.list_tools()
    tools = [tool_to_dict(tool) for tool in response.tools]
    if args.filter:
        term = args.filter.lower()
        tools = [
            tool
            for tool in tools
            if term
            in (
                str(tool.get("name", ""))
                + " "
                + str(tool.get("description", ""))
            ).lower()
        ]
    payload = {"count": len(tools), "tools": tools}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"已写入：{path.resolve()}")
    else:
        print(text)
    return 0


async def call_once(
    client: TikTokMcpClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    async with client.session() as session:
        result = await session.call_tool(tool_name, arguments=arguments)
    return result_to_dict(result)


def parse_advertisers(result: dict[str, Any]) -> list[dict[str, str]]:
    advertisers: list[dict[str, str]] = []
    for item in extract_rows(result):
        advertiser_id = str(
            item.get("advertiser_id")
            or item.get("id")
            or ""
        ).strip()
        if not advertiser_id:
            continue
        advertisers.append(
            {
                "advertiser_id": advertiser_id,
                "advertiser_name": str(
                    item.get("advertiser_name") or item.get("name") or ""
                ),
            }
        )
    return advertisers


def attach_advertiser(
    rows: list[dict[str, Any]],
    advertiser_id: str,
    advertiser_name: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        enriched.append(
            {
                "advertiser_id": advertiser_id,
                "advertiser_name": advertiser_name,
                **row,
            }
        )
    return enriched


async def fetch_report_pages(
    session: ClientSession,
    base_arguments: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page = int(base_arguments.get("page") or 1)
    page_size = int(base_arguments.get("page_size") or 1000)
    rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []

    while True:
        arguments = {
            **base_arguments,
            "page": page,
            "page_size": page_size,
        }
        result = result_to_dict(
            await session.call_tool(
                "report_integrated_get", arguments=arguments
            )
        )
        pages.append(result)
        if result.get("isError"):
            break

        page_rows = extract_rows(result)
        rows.extend(page_rows)

        page_info = extract_page_info(result)
        total_page = int(page_info.get("total_page") or 1)
        if page >= total_page or not page_rows:
            break
        page += 1

    return rows, pages


def filter_advertisers(
    advertisers: list[dict[str, str]],
    *,
    advertiser_ids: list[str] | None = None,
    advertiser_keyword: str | None = None,
) -> list[dict[str, str]]:
    filtered = advertisers
    if advertiser_ids:
        wanted = {item.strip() for item in advertiser_ids if item.strip()}
        filtered = [
            item for item in filtered if item["advertiser_id"] in wanted
        ]
    if advertiser_keyword:
        keyword = advertiser_keyword.lower()
        filtered = [
            item
            for item in filtered
            if keyword
            in (item["advertiser_id"] + " " + item["advertiser_name"]).lower()
        ]
    return filtered


def extract_page_info(result: dict[str, Any]) -> dict[str, Any]:
    parsed = result.get("parsed")
    if isinstance(parsed, dict):
        data = parsed.get("data")
        if isinstance(data, dict) and isinstance(data.get("page_info"), dict):
            return data["page_info"]
        if isinstance(parsed.get("page_info"), dict):
            return parsed["page_info"]
    return (
        (result.get("parsed") or {}).get("data", {}).get("page_info")
        or {}
    )


def normalize_creative_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        metrics = (
            row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        )
        if info or metrics:
            normalized.append(
                {
                    "info": info,
                    "metrics": metrics,
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {"info", "metrics"}
                    },
                }
            )
        else:
            normalized.append(row)
    return normalized


async def fetch_creative_report_pages(
    session: ClientSession,
    base_params: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page = int(base_params.get("page") or 1)
    page_size = int(base_params.get("page_size") or 1000)
    rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []

    while True:
        params = {
            **base_params,
            "page": page,
            "page_size": page_size,
        }
        result = result_to_dict(
            await session.call_tool(
                "tool_execute",
                arguments={
                    "tool_name": "creative_report_get",
                    "params": params,
                },
            )
        )
        pages.append(result)
        if result.get("isError"):
            break

        parsed = result.get("parsed")
        if isinstance(parsed, dict) and parsed.get("code") not in (0, None):
            break

        page_rows = normalize_creative_rows(extract_rows(result))
        rows.extend(page_rows)

        page_info = extract_page_info(result)
        total_page = int(page_info.get("total_page") or 1)
        if page >= total_page or not page_rows:
            break
        page += 1

    return rows, pages


async def fetch_all_advertiser_reports(
    client: TikTokMcpClient,
    report_arguments: dict[str, Any],
    *,
    advertiser_ids: list[str] | None = None,
    advertiser_keyword: str | None = None,
    only_active_rows: bool = False,
    only_spend_rows: bool = False,
    max_retries: int = 2,
) -> dict[str, Any]:
    async with client.session() as session:
        advertisers_result = result_to_dict(
            await session.call_tool("auth_advertiser_get", arguments={})
        )
        advertisers = filter_advertisers(
            parse_advertisers(advertisers_result),
            advertiser_ids=advertiser_ids,
            advertiser_keyword=advertiser_keyword,
        )

        all_rows: list[dict[str, Any]] = []
        account_summaries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for index, advertiser in enumerate(advertisers, start=1):
            advertiser_id = advertiser["advertiser_id"]
            advertiser_name = advertiser["advertiser_name"]
            print(
                f"[{index}/{len(advertisers)}] "
                f"{advertiser_name or advertiser_id} ...",
                flush=True,
            )
            arguments = {
                **report_arguments,
                "advertiser_id": advertiser_id,
            }
            arguments.pop("advertiser_ids", None)

            last_error: Any = None
            for attempt in range(1, max_retries + 2):
                try:
                    rows, pages = await fetch_report_pages(session, arguments)
                    if pages and pages[-1].get("isError"):
                        last_error = pages[-1]
                        if attempt <= max_retries:
                            print(
                                f"  重试 {attempt}/{max_retries} ...",
                                flush=True,
                            )
                            await asyncio.sleep(1.5 * attempt)
                            continue
                        errors.append(
                            {
                                "advertiser_id": advertiser_id,
                                "advertiser_name": advertiser_name,
                                "error": last_error,
                            }
                        )
                        break

                    enriched = attach_advertiser(
                        rows, advertiser_id, advertiser_name
                    )
                    if only_spend_rows:
                        enriched = filter_rows_with_activity(
                            enriched, only_spend=True
                        )
                    elif only_active_rows:
                        enriched = filter_rows_with_activity(enriched)

                    all_rows.extend(enriched)
                    account_summaries.append(
                        {
                            "advertiser_id": advertiser_id,
                            "advertiser_name": advertiser_name,
                            "row_count": len(enriched),
                        }
                    )
                    last_error = None
                    break
                except Exception as error:
                    last_error = str(error)
                    if attempt <= max_retries:
                        print(
                            f"  异常重试 {attempt}/{max_retries}: {error}",
                            flush=True,
                        )
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    errors.append(
                        {
                            "advertiser_id": advertiser_id,
                            "advertiser_name": advertiser_name,
                            "error": last_error,
                        }
                    )

    summary = build_summary(all_rows)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "advertiser_count": len(advertisers),
        "row_count": len(all_rows),
        "error_count": len(errors),
        "account_summaries": account_summaries,
        "errors": errors,
        "report_arguments": report_arguments,
        "summary": summary,
        "rows": all_rows,
    }


async def fetch_all_advertiser_creative_reports(
    client: TikTokMcpClient,
    creative_params: dict[str, Any],
    *,
    material_types: list[str] | None = None,
    advertiser_ids: list[str] | None = None,
    advertiser_keyword: str | None = None,
    only_active_rows: bool = False,
    only_spend_rows: bool = False,
    max_retries: int = 2,
) -> dict[str, Any]:
    types = material_types or ["VIDEO"]
    async with client.session() as session:
        advertisers_result = result_to_dict(
            await session.call_tool("auth_advertiser_get", arguments={})
        )
        advertisers = filter_advertisers(
            parse_advertisers(advertisers_result),
            advertiser_ids=advertiser_ids,
            advertiser_keyword=advertiser_keyword,
        )

        all_rows: list[dict[str, Any]] = []
        account_summaries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for index, advertiser in enumerate(advertisers, start=1):
            advertiser_id = advertiser["advertiser_id"]
            advertiser_name = advertiser["advertiser_name"]
            print(
                f"[{index}/{len(advertisers)}] "
                f"{advertiser_name or advertiser_id} ...",
                flush=True,
            )
            account_rows: list[dict[str, Any]] = []
            failed = False
            for material_type in types:
                params = {
                    **creative_params,
                    "advertiser_id": advertiser_id,
                    "material_type": material_type,
                }
                last_error: Any = None
                for attempt in range(1, max_retries + 2):
                    try:
                        rows, pages = await fetch_creative_report_pages(
                            session, params
                        )
                        last_page = pages[-1] if pages else {}
                        parsed = last_page.get("parsed")
                        api_error = (
                            isinstance(parsed, dict)
                            and parsed.get("code") not in (0, None)
                        )
                        if last_page.get("isError") or api_error:
                            last_error = last_page
                            if attempt <= max_retries:
                                print(
                                    f"  {material_type} 重试 "
                                    f"{attempt}/{max_retries} ...",
                                    flush=True,
                                )
                                await asyncio.sleep(1.5 * attempt)
                                continue
                            errors.append(
                                {
                                    "advertiser_id": advertiser_id,
                                    "advertiser_name": advertiser_name,
                                    "material_type": material_type,
                                    "error": last_error,
                                }
                            )
                            failed = True
                            break

                        enriched = attach_advertiser(
                            rows, advertiser_id, advertiser_name
                        )
                        for row in enriched:
                            row["material_type"] = material_type
                        account_rows.extend(enriched)
                        last_error = None
                        break
                    except Exception as error:
                        last_error = str(error)
                        if attempt <= max_retries:
                            print(
                                f"  {material_type} 异常重试 "
                                f"{attempt}/{max_retries}: {error}",
                                flush=True,
                            )
                            await asyncio.sleep(1.5 * attempt)
                            continue
                        errors.append(
                            {
                                "advertiser_id": advertiser_id,
                                "advertiser_name": advertiser_name,
                                "material_type": material_type,
                                "error": last_error,
                            }
                        )
                        failed = True
                        break
                if failed:
                    break

            if only_spend_rows:
                account_rows = filter_rows_with_activity(
                    account_rows, only_spend=True
                )
            elif only_active_rows:
                account_rows = filter_rows_with_activity(account_rows)

            all_rows.extend(account_rows)
            account_summaries.append(
                {
                    "advertiser_id": advertiser_id,
                    "advertiser_name": advertiser_name,
                    "row_count": len(account_rows),
                }
            )

    summary = build_summary(all_rows)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "advertiser_count": len(advertisers),
        "row_count": len(all_rows),
        "error_count": len(errors),
        "account_summaries": account_summaries,
        "errors": errors,
        "report_arguments": {
            "mode": "creative_material",
            "material_types": types,
            **creative_params,
        },
        "summary": summary,
        "rows": all_rows,
    }


async def command_call(args: argparse.Namespace) -> int:
    arguments = render_dynamic_values(parse_json_argument(args.args))
    result = await call_once(build_client(args), args.tool, arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output_dir:
        paths = save_result(
            Path(args.output_dir),
            args.tool,
            arguments,
            result,
            args.format,
        )
        for path in paths:
            print(f"已写入：{path.resolve()}")
    return 1 if result.get("isError") else 0


async def command_report_all(args: argparse.Namespace) -> int:
    mode = getattr(args, "mode", "basic") or "basic"
    if args.args:
        custom_arguments = render_dynamic_values(
            parse_json_argument(args.args)
        )
        preset = "custom_args"
        if mode == "material" or custom_arguments.get("mode") == "creative_material":
            mode = "material"
            creative_params = {
                key: value
                for key, value in custom_arguments.items()
                if key not in {"mode", "material_types"}
            }
            material_types = custom_arguments.get("material_types") or [
                getattr(args, "material_type", "VIDEO") or "VIDEO"
            ]
            if isinstance(material_types, str):
                material_types = [material_types]
            query_lifetime = bool(
                creative_params.get("lifetime")
                or creative_params.get("query_lifetime")
            )
            start_date = creative_params.get("start_date")
            end_date = creative_params.get("end_date")
            payload = await fetch_all_advertiser_creative_reports(
                build_client(args),
                creative_params,
                material_types=list(material_types),
                advertiser_ids=(
                    [
                        item.strip()
                        for item in (args.advertiser_id or "").split(",")
                        if item.strip()
                    ]
                    or None
                ),
                advertiser_keyword=args.advertiser_keyword,
                only_active_rows=args.only_active,
                only_spend_rows=args.only_spend,
                max_retries=args.retries,
            )
        else:
            report_arguments = custom_arguments
            query_lifetime = bool(report_arguments.get("query_lifetime"))
            start_date = report_arguments.get("start_date")
            end_date = report_arguments.get("end_date")
            payload = await fetch_all_advertiser_reports(
                build_client(args),
                report_arguments,
                advertiser_ids=(
                    [
                        item.strip()
                        for item in (args.advertiser_id or "").split(",")
                        if item.strip()
                    ]
                    or None
                ),
                advertiser_keyword=args.advertiser_keyword,
                only_active_rows=args.only_active,
                only_spend_rows=args.only_spend,
                max_retries=args.retries,
            )
    else:
        if args.lifetime:
            preset = "lifetime"
        elif getattr(args, "date", None):
            preset = "day"
            args.start_date = args.date
            args.end_date = args.date
        elif args.start_date and not args.end_date:
            preset = "day"
            args.end_date = args.start_date
        elif args.start_date and args.end_date:
            preset = "custom"
        else:
            preset = args.preset

        start_token, end_token, query_lifetime = resolve_date_preset(
            preset,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        advertiser_ids = None
        if args.advertiser_id:
            advertiser_ids = [
                item.strip()
                for item in args.advertiser_id.split(",")
                if item.strip()
            ]

        if mode == "material":
            creative_params = build_creative_report_params(
                material_type=getattr(args, "material_type", "VIDEO")
                or "VIDEO",
                start_date=start_token,
                end_date=end_token,
                query_lifetime=query_lifetime,
            )
            creative_params = render_dynamic_values(creative_params)
            start_date = creative_params.get("start_date")
            end_date = creative_params.get("end_date")
            material_types = [args.material_type or "VIDEO"]
            if getattr(args, "include_image_material", False):
                if "IMAGE" not in material_types:
                    material_types.append("IMAGE")
            payload = await fetch_all_advertiser_creative_reports(
                build_client(args),
                creative_params,
                material_types=material_types,
                advertiser_ids=advertiser_ids,
                advertiser_keyword=args.advertiser_keyword,
                only_active_rows=args.only_active,
                only_spend_rows=args.only_spend,
                max_retries=args.retries,
            )
        else:
            report_arguments = build_basic_report_arguments(
                data_level=args.data_level,
                start_date=start_token,
                end_date=end_token,
                query_lifetime=query_lifetime,
            )
            if query_lifetime:
                start_date = None
                end_date = None
            else:
                report_arguments = render_dynamic_values(report_arguments)
                start_date = report_arguments.get("start_date")
                end_date = report_arguments.get("end_date")

            payload = await fetch_all_advertiser_reports(
                build_client(args),
                report_arguments,
                advertiser_ids=advertiser_ids,
                advertiser_keyword=args.advertiser_keyword,
                only_active_rows=args.only_active,
                only_spend_rows=args.only_spend,
                max_retries=args.retries,
            )

    level_tag = "material" if mode == "material" else args.data_level.lower()
    label = default_output_stem(
        preset=preset,
        start_date=str(start_date) if start_date else None,
        end_date=str(end_date) if end_date else None,
        query_lifetime=query_lifetime
        or bool(
            (payload.get("report_arguments") or {}).get("query_lifetime")
            or (payload.get("report_arguments") or {}).get("lifetime")
        ),
    )
    label = f"{label}_{level_tag}"
    paths = save_rows(
        Path(args.output_dir),
        label,
        payload,
        payload["rows"],
        args.format,
    )
    totals = (payload.get("summary") or {}).get("totals") or {}
    print(
        json.dumps(
            {
                "mode": mode,
                "advertiser_count": payload["advertiser_count"],
                "row_count": payload["row_count"],
                "error_count": payload["error_count"],
                "spend": totals.get("spend"),
                "clicks": totals.get("clicks"),
                "conversions": totals.get("conversion"),
                "outputs": [str(path) for path in paths],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if payload["error_count"] and not payload["row_count"] else 0


async def command_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    interval = int(config.get("interval_seconds", 300))
    output_dir = Path(config.get("output_dir", "data"))
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    formats = config.get("formats", ["json"])
    if not isinstance(formats, list):
        raise ValueError("formats 必须是字符串数组")

    client = build_client(args)
    while True:
        started = time.monotonic()
        try:
            if config.get("all_advertisers"):
                report_arguments = render_dynamic_values(
                    config.get("arguments", {})
                )
                payload = await fetch_all_advertiser_reports(
                    client, report_arguments
                )
                paths = save_rows(
                    output_dir,
                    "all_advertisers_report",
                    payload,
                    payload["rows"],
                    formats,
                )
                status = (
                    "部分失败"
                    if payload["error_count"]
                    else "成功"
                )
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"{status}，账户 {payload['advertiser_count']}，"
                    f"行数 {payload['row_count']}，"
                    f"错误 {payload['error_count']}，"
                    f"输出：{', '.join(str(path) for path in paths) or '无'}",
                    flush=True,
                )
            else:
                tool_name = config["tool"]
                arguments = render_dynamic_values(config.get("arguments", {}))
                result = await call_once(client, tool_name, arguments)
                paths = save_result(
                    output_dir, tool_name, arguments, result, formats
                )
                status = "失败" if result.get("isError") else "成功"
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"{status}，输出：{', '.join(str(path) for path in paths) or '无'}",
                    flush=True,
                )
        except Exception as error:
            print(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"拉取失败：{error}",
                file=sys.stderr,
                flush=True,
            )
            if args.once:
                raise
        if args.once:
            return 0
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1, interval - elapsed))


async def command_logout(args: argparse.Namespace) -> int:
    storage = FileTokenStorage(Path(args.token_file))
    await storage.clear()
    print(f"已删除本地 OAuth 状态：{Path(args.token_file).expanduser()}")
    return 0


def add_connection_options(parser: argparse.ArgumentParser) -> None:
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
        "--proxy",
        default=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
        help="HTTP/HTTPS 代理，例如 http://127.0.0.1:7897",
    )
    parser.add_argument(
        "--callback-port",
        type=int,
        default=int(
            os.getenv("TIKTOK_MCP_CALLBACK_PORT", DEFAULT_CALLBACK_PORT)
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="仅打印授权 URL，不自动打开浏览器",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="发生错误时打印完整堆栈",
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="无需 Claude Code，直接调用 TikTok 官方远程 MCP"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="完成 OAuth 并验证连接")
    add_connection_options(auth_parser)
    auth_parser.set_defaults(handler=command_auth)

    tools_parser = subparsers.add_parser("tools", help="列出官方 MCP 工具")
    add_connection_options(tools_parser)
    tools_parser.add_argument("--filter", help="按名称和描述过滤")
    tools_parser.add_argument("--output", help="将工具定义保存为 JSON")
    tools_parser.set_defaults(handler=command_tools)

    call_parser = subparsers.add_parser("call", help="调用指定 MCP 工具")
    add_connection_options(call_parser)
    call_parser.add_argument("tool", help="工具名称")
    call_parser.add_argument(
        "--args",
        default="{}",
        help='JSON 对象，或使用 @文件路径，例如 --args "@args.json"',
    )
    call_parser.add_argument("--output-dir", help="保存结果的目录")
    call_parser.add_argument(
        "--format",
        action="append",
        choices=["json", "csv", "xlsx", "excel"],
        default=None,
        help="输出格式，可重复传入",
    )
    call_parser.set_defaults(handler=command_call)

    report_all_parser = subparsers.add_parser(
        "report-all",
        help="拉取全部/筛选广告账户的投流报表（支持时间范围/全量/Excel）",
    )
    add_connection_options(report_all_parser)
    report_all_parser.add_argument(
        "--args",
        help="自定义报表参数 JSON，或使用 @文件路径",
    )
    report_all_parser.add_argument(
        "--preset",
        default="last_7_days",
        help=(
            "时间预设: today/yesterday/day/last_7_days/last_14_days/"
            "last_30_days/this_month/lifetime/custom"
        ),
    )
    report_all_parser.add_argument(
        "--date",
        default=None,
        help="单日日期 YYYY-MM-DD（等价于 start_date=end_date=该日）",
    )
    report_all_parser.add_argument(
        "--start-date",
        default=None,
        help="开始日期 YYYY-MM-DD；只填此项时按单日拉取",
    )
    report_all_parser.add_argument(
        "--end-date",
        default=None,
        help="结束日期 YYYY-MM-DD；与 --start-date 组成区间",
    )
    report_all_parser.add_argument(
        "--lifetime",
        action="store_true",
        help="拉取 lifetime 指标（query_lifetime=true）",
    )
    report_all_parser.add_argument(
        "--data-level",
        default="AUCTION_AD",
        choices=[
            "AUCTION_CAMPAIGN",
            "AUCTION_ADGROUP",
            "AUCTION_AD",
            "AUCTION_ADVERTISER",
        ],
        help="basic 模式下的数据粒度；默认广告级（有消耗素材投放单元）",
    )
    report_all_parser.add_argument(
        "--mode",
        default="basic",
        choices=["basic", "material"],
        help="basic=计划/广告组/广告报表；material=按视频素材汇总消耗",
    )
    report_all_parser.add_argument(
        "--material-type",
        default="VIDEO",
        choices=["VIDEO", "IMAGE", "INSTANT_PAGE"],
        help="material 模式下的素材类型，默认 VIDEO",
    )
    report_all_parser.add_argument(
        "--include-image-material",
        action="store_true",
        help="material 模式下同时拉取 IMAGE 素材报表",
    )
    report_all_parser.add_argument(
        "--advertiser-id",
        help="只拉指定账户，多个用逗号分隔",
    )
    report_all_parser.add_argument(
        "--advertiser-keyword",
        help="按账户名/ID 关键字过滤",
    )
    report_all_parser.add_argument(
        "--only-active",
        action="store_true",
        help="只保留有曝光/点击/消耗/转化的行",
    )
    report_all_parser.add_argument(
        "--only-spend",
        action="store_true",
        help="只保留有消耗的行",
    )
    report_all_parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="单账户失败重试次数",
    )
    report_all_parser.add_argument(
        "--output-dir",
        default="data",
        help="保存目录",
    )
    report_all_parser.add_argument(
        "--format",
        action="append",
        choices=["json", "csv", "xlsx", "excel"],
        default=None,
        help="输出格式，可重复传入；默认 json,csv,xlsx",
    )
    report_all_parser.set_defaults(handler=command_report_all)

    run_parser = subparsers.add_parser("run", help="按配置持续拉取数据")
    add_connection_options(run_parser)
    run_parser.add_argument("config", help="任务配置 JSON")
    run_parser.add_argument(
        "--once", action="store_true", help="只执行一次，用于测试/任务计划"
    )
    run_parser.set_defaults(handler=command_run)

    logout_parser = subparsers.add_parser(
        "logout", help="删除本地 OAuth Token"
    )
    add_connection_options(logout_parser)
    logout_parser.set_defaults(handler=command_logout)
    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()
    if getattr(args, "format", None) is None:
        if getattr(args, "command", None) == "report-all":
            args.format = ["json", "csv", "xlsx"]
        else:
            args.format = ["json"]
    try:
        exit_code = asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        print("\n已停止。")
        exit_code = 130
    except Exception as error:
        if getattr(args, "debug", False):
            traceback.print_exc()
        else:
            print(f"错误：{error}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
