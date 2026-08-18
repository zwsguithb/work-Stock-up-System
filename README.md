# 备货系统（Replenishment System）

按款号（SKU）维度做备货/补货测算的 Web 系统。支持从领星（Lingxing）ERP 一键拉取销售与库存数据（或上传兜底 Excel），结合「款号账号配置」「未生产数目」等，自动计算各平台/账号的减噪后销量、预测单量、需要量与可售天数，并导出与《各款号备货》样例结构一致的 Excel。

## 功能

- **基准日模型**：销售数据取「基准日往前、不含基准日」；库存取实时。
- **减噪后销量**：按 近3/7/14/30/60 天加权（5%/15%/27%/28%/25%）。各平台销量窗口截止取「该平台最新销售日」与「基准日−1」的较小值。
- **历史日均销量**：最早销售日 ~ 基准日−1 的总销量 ÷ 相隔天数。
- **多平台**：亚马逊（按账号模糊匹配前 2 字母）、沃尔玛、Temu、其他平台（仅国内仓、不含海外仓）。
- **动态批次列**：按款号未生产数目生成批次列与 n+1 个可售天数列。
- **导出**：按款号生成多 Sheet Excel（汇总 + 各平台明细 + 未生产数目）。
- **按款号导出**：在工具栏输入款号后点「按款号导出」，仅导出该款号的全部数据表格（汇总 + 各平台明细 + 该款号未生产数目）；留空则导出全部款号。

## 目录结构

```
replenishment-system/
├── app/
│   ├── main.py         # FastAPI 入口
│   ├── database.py     # SQLite schema 与迁移
│   ├── calc.py         # 计算引擎（销量聚合/减噪/预测/可售天数）
│   ├── data_import.py  # Excel 销售/库存/配置/未生产数目 解析
│   ├── lingxing.py     # 领星 ERP 接口（一键出表）
│   └── api.py          # HTTP 接口
├── static/             # 前端（index.html / app.js / style.css）
├── data/               # 运行时 SQLite 与日志（不入库）
└── requirements.txt
```

## 环境要求

- Python 3.10+
- Windows / Linux 均可

## 安装与运行

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开浏览器访问 http://localhost:8000 。`data/` 目录会在首次导入 `database` 时自动创建。

## 使用流程

1. 在页面设置「基准日」「报表名称」，上传：款号账号配置、未生产数目、销售数据、库存数据。
2. 点击「一键领星出表」（需配置领星 AppId/AppSecret/sids）或「仅重算（用已有数据）」。
3. 查看汇总表与各平台明细，点击「导出 Excel」下载。

## 领星 ERP 配置

在「参数/领星配置」中填入 AppId、AppSecret、sids（公司/店铺 ID）。`app/lingxing.py` 负责鉴权与数据拉取。

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reports` | 报表列表 |
| POST | `/api/reports/{id}/generate` | 生成测算 |
| GET | `/api/reports/{id}/summary_table` | 汇总表 |
| GET | `/api/reports/{id}/detail` | 明细表 |
| GET | `/api/reports/{id}/export` | 导出 Excel（支持 `?style_code=款号` 仅导出该款号全部表格） |
| POST | `/api/lingxing/sync` | 领星一键出表 |

## 说明

- `data/`、调试/验证脚本（compare_*.py、verify_pipeline.py 等）已通过 `.gitignore` 排除，不进入版本库。
- 业务数据库 `replenishment.db` 在运行时由系统自动生成，无需随代码提交。
