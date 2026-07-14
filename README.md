# TikTok 官方 MCP 投流数据自动化客户端

不依赖 Claude Code，用 Python 脚本连接 **TikTok for Business 官方远程 MCP**，自动拉取广告账户的投流数据（消耗、曝光、点击、转化、ROAS 等），并导出 JSON / CSV。

适合：国内同事本地定时拉数、给业务/投放同学做报表底表。

---

## 一、这个项目在做什么

TikTok 官方提供了一个远程 MCP 服务（托管在 TikTok 侧）。本项目是一个**独立 MCP 客户端**：

1. 浏览器完成 TikTok OAuth 授权  
2. 调用官方 MCP 工具（底层仍是 Marketing API，但你**不用自己申请 App ID / Secret，也不用手写 Marketing API 请求**）  
3. 批量遍历当前账号下全部授权广告账户  
4. 拉取计划级投流报表，保存到本地 `data/`  

```text
本机 Python 客户端
    │  MCP (Streamable HTTP) + OAuth
    ▼
TikTok 官方 MCP
  https://business-api.tiktok.com/open_mcp/tt-ads-mcp-layer
    │  内部调用
    ▼
TikTok Marketing API / 广告账户数据
```

和 Claude Code 接入官方 MCP 是同一条官方能力；区别是这里用脚本自动化，不走对话。

---

## 二、能拿到什么数据

核心工具：

| 工具 | 作用 |
|------|------|
| `auth_advertiser_get` | 列出当前授权下的全部广告账户 |
| `report_integrated_get` | 同步综合报表（投流指标） |
| `report-all` 命令 | 自动遍历全部账户并合并结果 |

默认报表字段包括：

- 基础：`spend` / `impressions` / `clicks` / `ctr` / `cpc` / `cpm`
- 转化：`conversion` / `cost_per_conversion` / `conversion_rate`
- ROAS 相关：`complete_payment_roas` / `total_active_pay_roas` / `total_purchase_value` / `value_per_complete_payment` / `total_complete_payment_rate`

说明：

- 行数 =「有投放数据的计划数」，不是账户数。例如 41 个账户里可能只有部分账户在日期范围内有数据。
- 部分小程序 IAA 账户可能没有 Complete Payment 回传，此时 ROAS 字段会是 `0.00`，但消耗、点击等仍可用。

---

## 三、环境要求

| 项 | 说明 |
|----|------|
| 系统 | Windows 10/11（已实测） |
| Python | 3.11+（推荐 **conda** 环境） |
| 网络 | 国内必须开 HTTP 代理（如 Clash `7890`） |
| 账号 | 能登录 TikTok for Business，且 Business Center 已分配广告账户权限 |

官方 MCP 地址（默认）：

```text
https://business-api.tiktok.com/open_mcp/tt-ads-mcp-layer
```

---

## 四、安装

推荐使用 **conda**（Miniconda / Anaconda）。项目根目录已提供 `environment.yml` 与一键脚本。

### 方式 A：Conda（推荐）

在项目根目录打开 PowerShell：

```powershell
cd <本仓库路径>

# 一键创建环境并安装（默认环境名 tiktok-mcp）
powershell -ExecutionPolicy Bypass -File .\scripts\setup-conda.ps1
```

手动等价步骤：

```powershell
cd <本仓库路径>
conda env create -f environment.yml
conda activate tiktok-mcp
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

若环境已存在，只需激活后重装依赖：

```powershell
conda activate tiktok-mcp
python -m pip install -e ".[dev]"
```

激活后可直接用：

```powershell
conda activate tiktok-mcp
tiktok-mcp-client --help
tiktok-mcp-fetch          # 交互式采集菜单
python fetch_ads.py       # 同上
```

不想 activate 时：

```powershell
conda run -n tiktok-mcp tiktok-mcp-client --help
conda run -n tiktok-mcp python fetch_ads.py
```

后面文档里的 `$TK` 在 conda 下可写成：

```powershell
# 已 activate 时
$TK = "tiktok-mcp-client"

# 或未 activate
$TK = "conda run -n tiktok-mcp tiktok-mcp-client"
```

**推荐入口（交互菜单）**：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1
```

非交互一键拉近 7 天并导出 Excel：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1 -Once last_7_days
```

重建环境（干净重装）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-conda.ps1 -Force
```

### 方式 B：venv（备选）

若不用 conda，也可建普通虚拟环境（建议建在用户目录，避免部分磁盘权限拦截 `.exe/.pyd`）：

```powershell
cd <本仓库路径>
python -m venv "$env:USERPROFILE\.venvs\tiktok-official-mcp" --copies
& "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\python.exe" -m pip install -U pip
& "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\python.exe" -m pip install -e .
$TK = "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\tiktok-mcp-client.exe"
```

---

## 五、首次使用（完整流程）

### 1. 开代理

确认 Clash / v2ray 等已启动，记下 **HTTP 代理端口**（常见 `7890`）。  
本客户端用 `--proxy http://127.0.0.1:端口`，**不需要** Claude Code 那套 `NODE_USE_ENV_PROXY`。

### 2. 授权登录

```powershell
& $TK auth --proxy http://127.0.0.1:7890
```

- 会自动打开浏览器，登录 TikTok for Business 并点授权  
- 成功后本地写入 Token：`%USERPROFILE%\.tiktok_official_mcp\oauth.json`  
- **该文件含敏感凭证，禁止提交 Git、禁止发给别人**

浏览器没弹出来时：

```powershell
& $TK auth --proxy http://127.0.0.1:7890 --no-browser
```

把终端里的 URL 手动粘贴到浏览器。

### 3. 确认账户列表

```powershell
& $TK call auth_advertiser_get --proxy http://127.0.0.1:7890 --output-dir data --format json
```

应返回 `data.list` 里多个 `advertiser_id`。若是空数组 `[]`，说明当前登录账号没有广告账户权限，需换有权限的账号重新授权。

### 4. 推荐：交互菜单采集（按时间 / 全量 / Excel）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_fetch.ps1
# 或
conda activate tiktok-mcp
python fetch_ads.py --proxy http://127.0.0.1:7890
```

菜单能力：

- 按时间拉取：今天 / 昨天 / 近7/14/30天 / 本月 / 自定义日期
- 全量 lifetime 拉取
- 按账户关键字或指定账户 ID
- 选择粒度：计划 / 广告组 / 广告 / 账户
- 导出 **Excel / CSV / JSON**（Excel 含：总览、明细、账户汇总、错误）
- 只保留有消耗的行、失败自动重试、打开输出目录、改代理、重新授权

命令行等价示例：

```powershell
# 近 7 天，全账户，导出 xlsx+csv+json
& $TK report-all --proxy http://127.0.0.1:7890 --preset last_7_days --only-spend --format xlsx --format csv --format json

# 自定义日期
& $TK report-all --proxy http://127.0.0.1:7890 --start-date 2026-07-01 --end-date 2026-07-13 --format xlsx

# lifetime 全量
& $TK report-all --proxy http://127.0.0.1:7890 --lifetime --format xlsx

# 只拉某个账户
& $TK report-all --proxy http://127.0.0.1:7890 --preset yesterday --advertiser-keyword 何祥伟 --format xlsx
```

### 5. 也可用参数模板拉取

项目已提供参数模板 `arguments.report.example.json`（近 7 天、计划级、含 ROAS 字段）。

```powershell
& $TK report-all `
  --proxy http://127.0.0.1:7890 `
  --args "@arguments.report.example.json" `
  --output-dir data `
  --format xlsx `
  --format csv `
  --format json
```

输出示例：

```text
data/YYYYMMDD_HHMMSS_report_last_7_days_....xlsx
data/YYYYMMDD_HHMMSS_report_last_7_days_....csv
data/YYYYMMDD_HHMMSS_report_last_7_days_....json
```

CSV 每一行是一条计划数据，并带上 `advertiser_id` / `advertiser_name`。

### 5. 定时拉取

`job.json` 已配置为「全账户 + ROAS + 近 7 天」。可直接：

```powershell
# 只跑一次（适合任务计划程序）
& $TK run job.json --once --proxy http://127.0.0.1:7890

# 持续跑（默认间隔见 job.json 的 interval_seconds）
& $TK run job.json --proxy http://127.0.0.1:7890
```

Windows 任务计划程序示例：

```text
程序:
C:\Users\<你的用户名>\.venvs\tiktok-official-mcp\Scripts\tiktok-mcp-client.exe

参数:
run <仓库绝对路径>\job.json --once --proxy http://127.0.0.1:7890

起始于:
<仓库绝对路径>
```

注意：任务执行时代理软件必须已启动。

---

## 六、常用命令一览

| 命令 | 说明 |
|------|------|
| `auth` | OAuth 登录授权 |
| `logout` | 删除本机 Token（换号前先执行） |
| `tools` | 导出官方 MCP 工具列表到 `tools.json` |
| `call <工具名>` | 调用单个工具 |
| `report-all` | 遍历全部授权账户拉报表并合并 |
| `run job.json` | 按配置定时 / 单次执行 |

查看官方有哪些工具：

```powershell
& $TK tools --proxy http://127.0.0.1:7890 --output tools.json
& $TK tools --proxy http://127.0.0.1:7890 --filter report
```

只查某一个账户：

```powershell
& $TK call report_integrated_get `
  --args "@arguments.report.example.json" `
  --proxy http://127.0.0.1:7890 `
  --output-dir data `
  --format csv
```

（需在 JSON 里写死 `advertiser_id`）

---

## 七、配置说明

### 动态日期占位符

在 `arguments` / `job.json` 里可用：

| 占位符 | 含义 |
|--------|------|
| `${today}` | 今天 |
| `${yesterday}` | 昨天 |
| `${week_ago}` | 7 天前 |
| `${now}` | 当前本地时间 |

PowerShell 传参时不要用双引号包 `${today}`（会被当成空变量）。推荐写进 JSON 文件，用 `--args "@xxx.json"`。

### `job.json`（全账户模式）

```json
{
  "all_advertisers": true,
  "arguments": {
    "report_type": "BASIC",
    "data_level": "AUCTION_CAMPAIGN",
    "dimensions": ["campaign_id"],
    "metrics": ["spend", "complete_payment_roas", "..."],
    "start_date": "${week_ago}",
    "end_date": "${today}",
    "page_size": 1000
  },
  "interval_seconds": 1800,
  "output_dir": "data",
  "formats": ["json", "csv"]
}
```

`data_level` 可选：

- `AUCTION_CAMPAIGN`：计划级（默认）
- `AUCTION_ADGROUP`：广告组级
- `AUCTION_AD`：广告级
- `AUCTION_ADVERTISER`：账户汇总级

---

## 八、换账号登录

直接 `auth` 往往会沿用浏览器里已登录的 TikTok 会话，看起来“换不了号”。正确步骤：

```powershell
# 1. 清掉本机 Token
& $TK logout

# 2. 浏览器退出当前 TikTok for Business 账号（或用无痕窗口）

# 3. 重新授权
& $TK auth --proxy http://127.0.0.1:7890
```

---

## 九、目录结构

```text
official-tiktok-mcp-client/
├── README.md
├── pyproject.toml
├── arguments.report.example.json   # 报表参数模板（含 ROAS）
├── job.json / job.example.json     # 定时任务配置
├── src/tiktok_mcp_client/cli.py    # 主程序
├── tests/                          # 单元测试
└── data/                           # 输出目录（已 gitignore，勿提交）
```

不要提交：

- `data/`
- `tools.json`（体积大，可本地生成）
- `%USERPROFILE%\.tiktok_official_mcp\oauth.json`
- `.env`、本机虚拟环境

---

## 十、常见问题

### 1. `All connection attempts failed` / 超时

代理没开或端口错了。确认：

```powershell
--proxy http://127.0.0.1:7890
```

端口改成你本机实际 HTTP 端口。

### 2. `auth_advertiser_get` 返回空列表

当前 TikTok 账号没有可用广告账户。检查 Business Center 权限，或换有权限的账号重新 `logout` + `auth`。

### 3. 只有几个账户有 CSV 行

正常。`report-all` 会扫全部账户，但只有日期范围内**有投放数据**的计划才会出行。

### 4. ROAS 全是 0

接口字段已请求成功，但账户可能未回传 Complete Payment / 付费事件。先在 Ads Manager 对照同一账户同一天的 ROAS；若后台也没有，需要业务侧补事件。若后台有而这里没有，把后台用的指标名发出来再改 `metrics`。

### 5. OAuth 回调端口占用

```powershell
& $TK auth --proxy http://127.0.0.1:7890 --callback-port 33419
```

更换端口可能需要重新走一遍授权。

### 6. 数据不是秒级实时

官方报表本身有平台侧延迟；本工具是轮询拉取，适合分钟～小时级更新，不是行情式实时。

---

## 十一、原理补充（给想二次开发的同事）

1. 使用官方 Python `mcp` SDK 的 Streamable HTTP Client  
2. OAuth：动态客户端注册 + 本地 `127.0.0.1` 回调接收 `code`，Token 落盘  
3. `report-all`：先 `auth_advertiser_get` 拿账户列表，再对每个 `advertiser_id` 调 `report_integrated_get`（自动翻页），合并后写 CSV  
4. 不在代码里直连 `open_api/v1.3/...`；鉴权与路由由官方 MCP 完成  

官方文档入口（需登录 Business API 门户）：

- [TikTok Ads MCP Server](https://business-api.tiktok.com/portal/docs/tiktok-ads-mcp-server/v1.3)
- [How to connect](https://business-api.tiktok.com/portal/docs/how-to-connect-to-tiktok-for-business-mcp-server/v1.3)
- [Available tools](https://business-api.tiktok.com/portal/docs/available-tools-in-tiktok-for-business-mcp-server/v1.3)

---

## 十二、开发自测

```powershell
& "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\python.exe" -m pip install -e ".[dev]"
& "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\python.exe" -m pytest
```

---

## 快速上手（复制即可）

```powershell
cd <本仓库路径>
$TK = "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\tiktok-mcp-client.exe"

# 首次：安装（仅一次）
python -m venv "$env:USERPROFILE\.venvs\tiktok-official-mcp" --copies
& "$env:USERPROFILE\.venvs\tiktok-official-mcp\Scripts\python.exe" -m pip install -e .

# 授权
& $TK auth --proxy http://127.0.0.1:7890

# 拉全量投流数据（含 ROAS）
& $TK report-all --proxy http://127.0.0.1:7890 --args "@arguments.report.example.json" --output-dir data --format json --format csv
```

把 `7890` 换成你的代理端口。结果在 `data/` 目录。
