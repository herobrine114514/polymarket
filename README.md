# Polycast

面向 Polymarket 的本地预测研究与模拟交易看板，覆盖宏观政策、日常事件、小时级加密货币及 BTC 五分钟市场。支持 Codex / DeepSeek 分析、来源证据记录、预测审计、结算核对和风险限制。

**当前版本用于研究与模拟交易，不会自动提交真实订单。** `experiments/` 中的数值模型和 SDK 验证工具独立运行，尚未作为生产交易策略接入。历史回测结果不代表可实现的实盘收益。

## 功能

- 简洁的市场卡片、筛选、明暗主题、模拟权益曲线和运行状态。
- 每张市场卡片提供“立即审计预测”及报价刷新入口。
- 保留 Fed 宏观事件，支持日常事件及小时、五分钟加密货币研究。
- 记录证据版本、接收时间、模型调用预算、预测与官方结算；支持本地 Windows 计划任务管理。
- 独立的 BTC 因果特征 / LightGBM、慢周期校准、天气预报组合、挂单回放和资金流研究脚本。

## 安装与启动

需要 Python 3.11+。实时 WebSocket 采集另需 Node.js 22+；使用系统代理时建议 Node.js 24+。主程序除了 Windows 时区数据外，主要使用 Python 标准库。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m polycast status
.\.venv\Scripts\python.exe -m polycast serve --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765/>。`serve` 会启动采集与已配置的自动预测工作线程；配置模型后，自动预测和“立即审计预测”可能产生模型调用费用。界面可暂停五分钟自动预测、查看并调整每日调用上限。请先确认本地预算。

默认运行数据存储在 `.polycast/`，可用 `POLYCAST_DATA_DIR` 环境变量指定目录。该目录包含数据库、凭据和原始采集资料，不应提交到仓库。

## 模型配置

Codex 需要可用的本地 Codex CLI 及其登录配置。DeepSeek 密钥通过本地隐藏输入保存；Windows 使用当前用户的 DPAPI 加密。不要把密钥、钱包私钥或助记词写进源码、Issue 或聊天。

```powershell
.\.venv\Scripts\python.exe -m polycast deepseek-key set
.\.venv\Scripts\python.exe -m polycast provider deepseek --limit 24
# 或选择已登录的 Codex CLI
.\.venv\Scripts\python.exe -m polycast provider codex --limit 8
```

DeepSeek 模型标识以 `polycast/provider.py` 中的配置为准；当前配置为 `deepseek-v4-pro`。网页登录 Polymarket 并不等于取得 CLOB API 下单授权。本仓库不包含任何用户的账户凭据。

可选 RSS 来源见 [sources.example.json](sources.example.json)。使用前请确认来源许可和接口限制；新闻、行情和第三方内容不因本项目使用 MIT 而改变其权利归属。

## Windows 无窗口运行

激活已安装项目的虚拟环境后，按需执行：

```powershell
.\.venv\Scripts\Activate.ps1
.\InstallRealtime.ps1
```

该脚本注册当前用户的 `PolycastRealtime` 计划任务，以 `pythonw.exe` 无窗口运行，并配置登录启动及每分钟恢复触发。请勿与其他独立启动实例重复运行。任务状态与控制入口位于本地看板；安装任务不是启动看板的必需步骤。

## 离线研究与验证

数值研究依赖单独安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r experiments/numeric-requirements.lock.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$nodeTests = Get-ChildItem tests -Filter '*.test.mjs' | ForEach-Object FullName
node --test $nodeTests
```

缺少数值研究库或可选 SDK 时，相关测试会明确跳过。SDK 实验依赖本地固定版本 `@polymarket/client@0.12.0` 和 `ethers@6.17.0`，相关安装目录及历史冻结样本不随源码发布。核心程序不依赖这些 SDK，也不会发送真实订单。完整的本地研究环境比首次克隆环境运行更多可选测试。

主要入口：

| 研究 | 文件 |
| --- | --- |
| BTC 五分钟历史数据与因果特征 | `experiments/btc5m_backfill.py`、`btc5m_edge_dataset.py` |
| BTC 数值模型、市场比较与回放 | `experiments/btc5m_edge_model.py` |
| 慢周期市场校准 | `experiments/slow_market_backfill.py`、`slow_market_calibration.py` |
| 天气数据与概率模型 | `experiments/weather_edge.py`、`weather_edge_model.py` |
| 天气挂单、结算与资金流研究 | `experiments/weather_maker_backtest.py`、`weather_settlement_edge.py`、`weather_flow_following.py` |

各脚本通过 `--help` 查看参数。回填脚本会访问公开 API，可能产生大量请求；请遵守数据服务的条款和速率限制。原始数据、模型产物及个人运行报告未公开。

[更新审阅说明](docs/update-review.zh-CN.md)记录模型验证的已知局限。正式采用任何策略前，需要冻结模型与参数，用之后首次收到的数据按事件进行前向验证，并计入费用、深度、延迟、资金占用及未成交。

## 目录

- `polycast/`：应用、看板、采集、证据、模拟交易与风险控制。
- `experiments/`：独立研究与离线执行验证工具。
- `tests/`：单元测试和合成测试样本。
- `docs/`：公开更新审阅与发布说明。
- `licenses/`：第三方内容归属说明。

## 许可证

项目源码以 [MIT License](LICENSE) 开源。第三方库和数据仍遵循各自许可证；相关说明见 [licenses/](licenses/)。
