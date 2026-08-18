import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import database as db

# 权重配置（稳定期）
WEIGHTS = {
    "d3": 0.05,
    "d7": 0.15,
    "d14": 0.27,
    "d30": 0.28,
    "d60": 0.25,
}


def date_to_str(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def str_to_date(s: str) -> datetime:
    s = str(s).strip().replace("-", "").replace("/", "")
    if len(s) == 8:
        return datetime.strptime(s, "%Y%m%d")
    raise ValueError(f"unsupported date format: {s}")


def round_qty(v: float) -> int:
    """按10的倍数四舍五入"""
    if v <= 0:
        return 0
    return int(round(v / 10)) * 10


def ceil_day(v: float) -> int:
    """向上取整，v<=0 返回 0（用于可售天数）"""
    if v <= 0:
        return 0
    return math.ceil(v)


def round_day(v: float) -> int:
    """四舍五入取整，v<=0 返回 0（明细表「可售天数」列按样例使用四舍五入）"""
    if v <= 0:
        return 0
    return int(math.floor(v + 0.5))


def get_date_range_windows(baseline_date: str) -> Dict[str, tuple]:
    """以 baseline_date 前一天为截止，往前推 3/7/14/30/60 天窗口 (含起止)"""
    base = str_to_date(baseline_date)
    end = base - timedelta(days=1)
    return {
        "d3": (end - timedelta(days=2), end),
        "d7": (end - timedelta(days=6), end),
        "d14": (end - timedelta(days=13), end),
        "d30": (end - timedelta(days=29), end),
        "d60": (end - timedelta(days=59), end),
    }


def get_date_range_windows_from_end(end_dt: datetime) -> Dict[str, tuple]:
    """以给定截止日期 end_dt 为准，往前推 3/7/14/30/60 天窗口 (含起止)"""
    return {
        "d3": (end_dt - timedelta(days=2), end_dt),
        "d7": (end_dt - timedelta(days=6), end_dt),
        "d14": (end_dt - timedelta(days=13), end_dt),
        "d30": (end_dt - timedelta(days=29), end_dt),
        "d60": (end_dt - timedelta(days=59), end_dt),
    }


def calc_denoised_sales(quantities: Dict[str, float], is_temu: bool = False) -> float:
    """计算减噪后销量 V"""
    if is_temu:
        return (quantities.get("d30", 0.0) or 0.0) / 30.0
    total = 0.0
    for k, w in WEIGHTS.items():
        days = int(k[1:])
        total += (quantities.get(k, 0.0) or 0.0) / days * w
    return total


# ---------------- 缓存（单次报表生成内复用，避免全表重复扫描） ----------------
_SALES_CACHE: Dict[tuple, Dict] = {}
_INV_CACHE: Dict[tuple, Dict] = {}


def clear_caches():
    _SALES_CACHE.clear()
    _INV_CACHE.clear()


def agg_sales_cached(conn, baseline_date: str, platform: str, account: Optional[str] = None):
    key = (baseline_date, platform, account)
    if key not in _SALES_CACHE:
        _SALES_CACHE[key] = agg_sales_by_sku(conn, baseline_date, platform, account)
    return _SALES_CACHE[key]


def inventory_cached(conn, platform: str, account: Optional[str] = None):
    key = (platform, account)
    if key not in _INV_CACHE:
        _INV_CACHE[key] = get_inventory_by_sku(conn, platform, account)
    return _INV_CACHE[key]


def agg_sales_by_sku(
    conn,
    baseline_date: str,
    platform: str,
    account: Optional[str] = None,
) -> Dict[str, Dict[str, float]]:
    """汇总某个平台/账号的 SKU 销量，返回 {sku: {d3,d7,d14,d30,d60,historical_total,historical_days}}

    窗口截止日 = min(基准日前一天, 该平台/账号实际可拿到的最新销售日期)。
    理由：各平台数据同步时间不同（如沃尔玛/其他平台最新数据可能早于基准日），
    若统一以 baseline-1 为截止，会把“最近 N 天”窗口往后平移、漏掉最近的实际销量。
    样例《各款号备货》正是以各平台“最新可销售日期”为窗口截止来统计的。
    """
    base_dt = str_to_date(baseline_date)
    baseline_cutoff = base_dt - timedelta(days=1)  # 硬性上限：不取基准日及以后的数据

    cur = conn.cursor()
    if account:
        cur.execute(
            "SELECT date, sku, quantity FROM sales_raw WHERE platform=? AND account=? AND date <= ?",
            (platform, account, date_to_str(baseline_cutoff)),
        )
    else:
        cur.execute(
            "SELECT date, sku, quantity FROM sales_raw WHERE platform=? AND account IS NULL AND date <= ?",
            (platform, date_to_str(baseline_cutoff)),
        )
    rows = cur.fetchall()
    if not rows:
        return {}

    # 该平台/账号实际可拿到的最新销售日期
    max_d_int = max(int(r[0]) for r in rows)
    platform_max_dt = str_to_date(str(max_d_int))
    eff_end_dt = min(baseline_cutoff, platform_max_dt)
    eff_end_int = int(date_to_str(eff_end_dt))
    windows = get_date_range_windows_from_end(eff_end_dt)

    result: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sku_dates: Dict[str, set] = defaultdict(set)
    for row in rows:
        d, sku, qty = row
        d_int = int(d)
        if platform == "temu":
            # temu 仅有「近30天销量」汇总列、无逐项日期，直接归入 d30 窗口
            d_int = eff_end_int
        if d_int > eff_end_int:
            continue
        result[sku]["historical_total"] = result[sku].get("historical_total", 0.0) + (qty or 0)
        sku_dates[sku].add(d_int)
        for key, (start, end) in windows.items():
            start_int = int(date_to_str(start))
            end_int = int(date_to_str(end))
            if start_int <= d_int <= end_int:
                result[sku][key] = result[sku].get(key, 0.0) + (qty or 0)

    # 历史销量日均 = 最早有销量日期 ~ 实际最新销售日期 的销量汇总 / 相隔天数
    for sku, dates in sku_dates.items():
        if dates:
            min_d = min(dates)
            days_diff = (eff_end_dt - str_to_date(str(min_d))).days + 1
            if days_diff < 1:
                days_diff = 1
            result[sku]["historical_days"] = days_diff
            result[sku]["historical_first_date"] = min_d
            result[sku]["historical_daily"] = (
                result[sku].get("historical_total", 0.0) / days_diff
            )
        else:
            result[sku]["historical_days"] = 0
            result[sku]["historical_daily"] = 0.0

    return dict(result)


def get_inventory_by_sku(
    conn,
    platform: str,
    account: Optional[str] = None,
) -> Dict[str, float]:
    """取最新库存，按 sku 汇总"""
    cur = conn.cursor()
    if account:
        cur.execute(
            "SELECT sku, SUM(quantity) FROM inventory_raw WHERE platform=? AND account=? GROUP BY sku",
            (platform, account),
        )
    else:
        cur.execute(
            "SELECT sku, SUM(quantity) FROM inventory_raw WHERE platform=? AND account IS NULL GROUP BY sku",
            (platform,),
        )
    return {sku: (qty or 0.0) for sku, qty in cur.fetchall()}


def get_unproduced(conn, style_code: str, sku: str) -> List[Dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT batch_name, quantity FROM unproduced_batches WHERE style_code=? AND sku=? ORDER BY batch_name",
        (style_code, sku),
    )
    return [{"batch_name": n, "quantity": q} for n, q in cur.fetchall()]


def get_all_unproduced_for_style(conn, style_code: str) -> Dict[str, List[Dict]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT sku, batch_name, quantity FROM unproduced_batches WHERE style_code=? ORDER BY sku, batch_name",
        (style_code,),
    )
    out: Dict[str, List[Dict]] = defaultdict(list)
    for sku, bn, q in cur.fetchall():
        out[sku].append({"batch_name": bn, "quantity": q})
    return dict(out)


def _get_cfg(conn, key: str, default: str) -> str:
    cur = conn.cursor()
    cur.execute("SELECT value FROM system_config WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] not in (None, "") else default


def generate_report(report_id: int, baseline_date: str) -> dict:
    clear_caches()
    conn = db.get_conn()
    cur = conn.cursor()

    # 可售天数口径：divisor = denoised(需求说明书) | forecast(对齐样例文件)
    #              rounding = ceil(需求说明书) | round(对齐样例文件)
    sd_divisor = _get_cfg(conn, "sellable_days_divisor", "denoised")
    sd_rounding = _get_cfg(conn, "sellable_days_rounding", "ceil")

    def _round_day(v: float) -> int:
        if v <= 0:
            return 0
        return math.ceil(v) if sd_rounding == "ceil" else int(round(v))

    # 1. 加载款号配置
    cur.execute("SELECT style_code, lifecycle FROM styles")
    styles = {row[0]: row[1] for row in cur.fetchall()}

    # 2. 加载账号配置
    cur.execute(
        "SELECT style_code, platform, account_name, account_prefix, stocking_days, quota FROM style_accounts ORDER BY sort_order"
    )
    style_accounts: Dict[str, List[Dict]] = defaultdict(list)
    for row in cur.fetchall():
        style_accounts[row[0]].append(
            {
                "platform": row[1],
                "account_name": row[2],
                "account_prefix": row[3],
                "stocking_days": row[4],
                "quota": row[5] or 0,
            }
        )

    if not styles:
        conn.close()
        return {"status": "error", "message": "没有款号配置，请先上传款号账号配置"}

    # 3. 汇总所有 SKU（从 sales + inventory + unproduced 取并集）
    cur.execute("SELECT DISTINCT sku FROM sales_raw")
    all_skus = {row[0] for row in cur.fetchall()}
    cur.execute("SELECT DISTINCT sku FROM inventory_raw")
    all_skus |= {row[0] for row in cur.fetchall()}
    cur.execute("SELECT DISTINCT sku FROM unproduced_batches")
    all_skus |= {row[0] for row in cur.fetchall()}

    # 4. 样式 -> SKU 关系（从 SKU 前缀推导）
    def style_of_sku(sku: str) -> Optional[str]:
        for sc in sorted(styles.keys(), key=len, reverse=True):
            if sku.startswith(sc):
                return sc
        return None

    sku_style = {sku: style_of_sku(sku) for sku in all_skus}

    # 预计算：每个账号下所有 SKU 的 V 之和（用于配额分配）
    account_v_sums: Dict[str, float] = defaultdict(float)
    account_v_by_sku: Dict[str, Dict[str, float]] = defaultdict(dict)
    temu_v_sums: Dict[str, float] = defaultdict(float)  # per style
    temu_v_by_sku: Dict[str, Dict[str, float]] = defaultdict(dict)

    for sku in all_skus:
        sc = sku_style.get(sku)
        if not sc:
            continue
        for acc in style_accounts.get(sc, []):
            if acc["platform"] == "amazon":
                sales = agg_sales_cached(conn, baseline_date, "amazon", acc["account_prefix"])
                q = sales.get(sku, {})
                v = calc_denoised_sales(q)  # 若需要历史销量优先可在此切换
                key = f"amazon:{acc['account_prefix']}"
                account_v_sums[key] += v
                account_v_by_sku[key][sku] = v
            elif acc["platform"] == "temu":
                sales = agg_sales_cached(conn, baseline_date, "temu")
                q = sales.get(sku, {})
                v = calc_denoised_sales(q, is_temu=True)
                temu_v_sums[sc] += v
                temu_v_by_sku[sc][sku] = v

    # 5. 逐 SKU 计算
    summary_rows = []
    detail_rows = []

    for sku in sorted(all_skus):
        sc = sku_style.get(sku)
        if not sc:
            continue
        # 颜色从 SKU 中段首字母推断，B=黑色 Z=肤色 W=白色
        color_code = sku[len(sc)] if sku.startswith(sc) and len(sku) > len(sc) else ""
        color_map = {"B": "黑色", "Z": "肤色", "W": "白色"}
        color = color_map.get(color_code, color_code)
        lifecycle = styles.get(sc, "")
        accs = style_accounts.get(sc, [])

        # 未生产批次
        batches = get_unproduced(conn, sc, sku)
        batch_qty_map = {b["batch_name"]: b["quantity"] for b in batches}
        total_unproduced = sum(b["quantity"] for b in batches)

        # 平台明细计算
        platform_needs = defaultdict(float)
        sku_detail = []
        denoised_total = 0.0
        final_total = 0.0
        fba_total = 0.0
        china_total = 0.0

        for acc in accs:
            platform = acc["platform"]
            account = acc["account_prefix"]
            stocking_days = acc["stocking_days"]
            quota = acc["quota"] or 0

            sales = agg_sales_cached(conn, baseline_date, platform, account if platform == "amazon" else None)
            q = sales.get(sku, {})
            inv = inventory_cached(conn, platform, account if platform == "amazon" else None)
            inv_qty = inv.get(sku, 0.0)

            if platform == "temu":
                denoised = calc_denoised_sales(q, is_temu=True)
                # temu 配额若配置则按比例分配，否则直接用 V
                if quota and temu_v_sums[sc] > 0:
                    forecast = denoised / temu_v_sums[sc] * quota
                else:
                    forecast = denoised
            elif platform == "amazon":
                denoised = calc_denoised_sales(q)
                key = f"amazon:{account}"
                if quota and account_v_sums[key] > 0:
                    forecast = denoised / account_v_sums[key] * quota
                else:
                    forecast = denoised
            else:
                # walmart / other
                denoised = calc_denoised_sales(q)
                forecast = denoised

            final_forecast = forecast  # 默认可人工覆盖
            forecast_qty = round(final_forecast * stocking_days)

            if platform == "amazon":
                need = max(forecast_qty - inv_qty, 0)
                fba_total += inv_qty
            elif platform == "walmart":
                need = max(forecast_qty - inv_qty, 0)
            elif platform == "temu":
                need = max(forecast_qty - inv_qty, 0)
            else:
                # other 不减库存
                need = forecast_qty

            denoised_total += denoised
            final_total += final_forecast
            platform_needs[platform] = platform_needs.get(platform, 0.0) + need
            if platform == "amazon":
                platform_needs[account] = platform_needs.get(account, 0.0) + need

            # 中国仓库存单列（Other 平台时， inventory 里 account IS NULL 的 other 仓 = 中国仓）
            if platform == "other":
                china_total += inv_qty

            sku_detail.append(
                {
                    "report_id": report_id,
                    "sku": sku,
                    "style_code": sc,
                    "color": color,
                    "platform": platform,
                    "account": account if platform == "amazon" else None,
                    "stocking_days": stocking_days,
                    "d3": q.get("d3", 0),
                    "d7": q.get("d7", 0),
                    "d14": q.get("d14", 0),
                    "d30": q.get("d30", 0),
                    "d60": q.get("d60", 0),
                    "denoised_sales": denoised,
                    "historical_sales": q.get("historical_total", 0),
                    "historical_days": int(q.get("historical_days", 0) or 0),
                    "historical_daily": round(q.get("historical_daily", 0.0) or 0.0, 6),
                    "forecast_sales": forecast,
                    "final_forecast": final_forecast,
                    "forecast_qty": forecast_qty,
                    "inventory_qty": inv_qty,
                    "need_qty": need,
                    "sellable_days": round_day(inv_qty / denoised) if denoised > 0 else 0,
                    "manual_final_forecast": None,
                }
            )

        # 汇总行
        total_need = sum(platform_needs.get(p, 0) for p in ["amazon", "walmart", "other", "temu"])
        available = china_total + total_unproduced
        if total_need <= 0:
            total_planned = -available
        else:
            total_planned = total_need - available

        current_order = round_qty(total_planned)

        # 可售天数 n+1 列：（中国仓+FBA[+累计批次]）/ 日均销量
        divisor = final_total if sd_divisor == "forecast" else denoised_total
        sellable_days_list = []
        if divisor > 0:
            base_stock = fba_total + china_total
            sellable_days_list.append(_round_day(base_stock / divisor))
            running = base_stock
            for b in batches:
                running += b["quantity"]
                sellable_days_list.append(_round_day(running / divisor))
        else:
            sellable_days_list = ["——"] * (len(batches) + 1)

        # 汇总备货天数 = 该款各账号中最长的备货周期
        summary_stocking_days = max((a["stocking_days"] or 0) for a in accs) if accs else None

        summary_rows.append(
            {
                "report_id": report_id,
                "sku": sku,
                "style_code": sc,
                "color": color,
                "lifecycle": lifecycle,
                "stocking_days": summary_stocking_days,
                "denoised_total_sales": round(denoised_total, 6),
                "final_forecast_total": round(final_total, 6),
                "fba_total": fba_total,
                "china_total": china_total,
                "batch_quantities": json.dumps(batch_qty_map, ensure_ascii=False),
                "sellable_days": json.dumps(sellable_days_list, ensure_ascii=False),
                "hw_need": platform_needs.get("HW", 0),
                "ps_need": platform_needs.get("PS", 0),
                "zo_need": platform_needs.get("ZO", 0),
                "other_need": platform_needs.get("other", 0),
                "temu_need": platform_needs.get("temu", 0),
                "walmart_need": platform_needs.get("walmart", 0),
                "total_need": total_need,
                "available_inventory": available,
                "total_planned_order": total_planned,
                "current_order_quantity": current_order,
                "manual_final_forecast": None,
            }
        )
        detail_rows.extend(sku_detail)

    # 6. 写入数据库
    cur.execute("DELETE FROM report_summary WHERE report_id=?", (report_id,))
    cur.execute("DELETE FROM report_detail WHERE report_id=?", (report_id,))

    for row in summary_rows:
        cur.execute(
            """INSERT INTO report_summary
            (report_id, sku, style_code, color, lifecycle, stocking_days, denoised_total_sales,
             final_forecast_total, fba_total, china_total, batch_quantities, sellable_days,
             hw_need, ps_need, zo_need, other_need, temu_need, walmart_need, total_need,
             available_inventory, total_planned_order, current_order_quantity, manual_final_forecast)
            VALUES (:report_id, :sku, :style_code, :color, :lifecycle, :stocking_days, :denoised_total_sales,
                    :final_forecast_total, :fba_total, :china_total, :batch_quantities, :sellable_days,
                    :hw_need, :ps_need, :zo_need, :other_need, :temu_need, :walmart_need, :total_need,
                    :available_inventory, :total_planned_order, :current_order_quantity, :manual_final_forecast)""",
            row,
        )

    for row in detail_rows:
        cur.execute(
            """INSERT INTO report_detail
            (report_id, sku, style_code, color, platform, account, stocking_days, d3, d7, d14, d30, d60,
             denoised_sales, historical_sales, historical_days, historical_daily,
             forecast_sales, final_forecast, forecast_qty,
             inventory_qty, need_qty, sellable_days, manual_final_forecast)
            VALUES (:report_id, :sku, :style_code, :color, :platform, :account, :stocking_days, :d3, :d7, :d14, :d30, :d60,
                    :denoised_sales, :historical_sales, :historical_days, :historical_daily,
                    :forecast_sales, :final_forecast, :forecast_qty,
                    :inventory_qty, :need_qty, :sellable_days, :manual_final_forecast)""",
            row,
        )

    cur.execute(
        "UPDATE reports SET status=?, updated_at=? WHERE id=?",
        ("completed", db.now(), report_id),
    )
    conn.commit()
    conn.close()

    return {
        "status": "completed",
        "report_id": report_id,
        "sku_count": len(summary_rows),
    }


def get_report_summary(conn, report_id: int) -> List[Dict]:
    cur = conn.cursor()
    cur.execute(
        """SELECT sku, style_code, color, lifecycle, stocking_days, denoised_total_sales, final_forecast_total,
                  fba_total, china_total, batch_quantities, sellable_days, hw_need, ps_need, zo_need,
                  other_need, temu_need, walmart_need, total_need, available_inventory,
                  total_planned_order, current_order_quantity
           FROM report_summary WHERE report_id=? ORDER BY sku""",
        (report_id,),
    )
    cols = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["batch_quantities"] = json.loads(d["batch_quantities"] or "{}")
        d["sellable_days"] = json.loads(d["sellable_days"] or "[]")
        rows.append(d)
    return rows


def get_report_detail(conn, report_id: int, platform: str, account: Optional[str] = None) -> List[Dict]:
    cur = conn.cursor()
    fields = """sku, style_code, color, platform, account, stocking_days, d3, d7, d14, d30, d60,
                denoised_sales, historical_sales, historical_days, historical_daily,
                forecast_sales, final_forecast, forecast_qty, inventory_qty, need_qty, sellable_days"""
    if platform == "amazon" and account:
        cur.execute(
            f"SELECT {fields} FROM report_detail WHERE report_id=? AND platform=? AND account=? ORDER BY sku",
            (report_id, platform, account),
        )
    else:
        cur.execute(
            f"SELECT {fields} FROM report_detail WHERE report_id=? AND platform=? ORDER BY sku",
            (report_id, platform),
        )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_style_batches(conn, style_code: str) -> List[str]:
    """该款号的未生产批次名（有序）"""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT batch_name FROM unproduced_batches WHERE style_code=? ORDER BY batch_name",
        (style_code,),
    )
    return [r[0] for r in cur.fetchall()]


def get_report_styles(conn, report_id: int) -> List[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT style_code FROM report_summary WHERE report_id=? ORDER BY style_code",
        (report_id,),
    )
    return [r[0] for r in cur.fetchall() if r[0]]


def summary_columns(batch_names: List[str]) -> List[str]:
    """按样例《各款号备货》汇总表列序生成表头"""
    cols = ["sku", "生命周期", "颜色", "备货天数", "减噪后总销量", "最终预测销量-人工调整",
            "中国仓+FBA可售天数"]
    acc = []
    for bn in batch_names:
        acc.append(bn)
        cols.append("中国仓+FBA+" + "+".join(acc) + "可售天数")
    cols += ["HW需要量", "PS需要量", "ZO需要量", "其他平台(不含temu，沃尔玛)需要量",
             "temu需要量", "沃尔玛需要量", "总需要量",
             "FBA库存", "中国仓库存", "中国仓+FBA"]
    cols += list(batch_names)
    cols += ["剩余未生产数", "中国仓库存+计划库存", "总计划下单数", "本次下单数量"]
    return cols


def summary_row_values(row: Dict, batch_names: List[str]) -> List:
    sellable = row["sellable_days"] if isinstance(row["sellable_days"], list) else json.loads(row["sellable_days"] or "[]")
    bq = row["batch_quantities"] if isinstance(row["batch_quantities"], dict) else json.loads(row["batch_quantities"] or "{}")
    vals = [row["sku"], row["lifecycle"], row["color"], row["stocking_days"],
            row["denoised_total_sales"], row["final_forecast_total"]]
    # 可售天数：n+1 列，缺失补 ——
    need = len(batch_names) + 1
    sd = list(sellable) + ["——"] * max(0, need - len(sellable))
    vals += sd[:need]
    vals += [row["hw_need"], row["ps_need"], row["zo_need"], row["other_need"],
             row["temu_need"], row["walmart_need"], row["total_need"],
             row["fba_total"], row["china_total"],
             (row["fba_total"] or 0) + (row["china_total"] or 0)]
    vals += [bq.get(bn, 0) for bn in batch_names]
    vals += [sum(bq.values()), row["available_inventory"],
             row["total_planned_order"], row["current_order_quantity"]]
    return vals


# 明细表：各平台的列定义 (显示名, 数据字段)
_W = {"d3": "近3天销量(5%)", "d7": "近7天销量(15%)", "d14": "近14天销量（27%）",
      "d30": "近30天销量(28%)", "d60": "近60天销量(25%)"}


def detail_columns(platform: str, account: Optional[str] = None) -> List[tuple]:
    base = [("sku", "sku"), ("颜色", "color"), ("备货天数", "stocking_days")]
    if platform == "temu":
        return base + [
            ("近30天销量", "d30"), ("预测销量", "forecast_sales"),
            ("最终预测销量-人工调整", "final_forecast"), ("预测单量", "forecast_qty"),
            ("需要量", "need_qty"), ("temu库存", "inventory_qty"),
        ]
    win = [(_W[k], k) for k in ["d3", "d7", "d14", "d30", "d60"]]
    if platform == "amazon":
        return base + win + [
            ("减噪后销量", "denoised_sales"),
            ("该账号下历史销量", "historical_daily"),
            ("预测销量", "forecast_sales"),
            ("最终预测销量-人工调整", "final_forecast"),
            ("预测单量", "forecast_qty"),
            (f"{account}需要量" if account else "需要量", "need_qty"),
            (f"亚马逊库存({account}账号)" if account else "亚马逊库存", "inventory_qty"),
            ("可售天数", "sellable_days"),
        ]
    if platform == "walmart":
        return base + win + [
            ("减噪后销量", "denoised_sales"), ("预测销量", "forecast_sales"),
            ("最终预测销量-人工调整", "final_forecast"), ("预测单量", "forecast_qty"),
            ("需要量", "need_qty"), ("沃尔玛库存", "inventory_qty"),
        ]
    # other：中国仓发货，不减库存，无库存列/可售天数列
    return base + win + [
        ("减噪后销量", "denoised_sales"), ("预测销量", "forecast_sales"),
        ("最终预测销量-人工调整", "final_forecast"), ("预测单量", "forecast_qty"),
        ("需要量", "need_qty"),
    ]


PLATFORM_LABELS = {
    "amazon": "{style}({account}账号)",
    "other": "{style}（其他平台，不含temu,沃尔玛）",
    "walmart": "{style}（沃尔玛）",
    "temu": "{style}（temu）",
}


def get_report_detail_by_style(conn, report_id: int, style_code: str,
                              platform: str, account: Optional[str] = None) -> List[Dict]:
    cur = conn.cursor()
    fields = """sku, style_code, color, platform, account, stocking_days, d3, d7, d14, d30, d60,
                denoised_sales, historical_sales, historical_days, historical_daily,
                forecast_sales, final_forecast, forecast_qty, inventory_qty, need_qty, sellable_days"""
    if platform == "amazon" and account:
        cur.execute(
            f"SELECT {fields} FROM report_detail WHERE report_id=? AND style_code=? AND platform=? AND account=? ORDER BY sku",
            (report_id, style_code, platform, account),
        )
    else:
        cur.execute(
            f"SELECT {fields} FROM report_detail WHERE report_id=? AND style_code=? AND platform=? ORDER BY sku",
            (report_id, style_code, platform),
        )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_report_summary_by_style(conn, report_id: int, style_code: str) -> List[Dict]:
    return [r for r in get_report_summary(conn, report_id) if r["style_code"] == style_code]


def get_detail_groups(conn, report_id: int, style_code: str) -> List[Dict]:
    """该款号在报表中的平台/账号分组，按亚马逊账号→其他→沃尔玛→temu 排序"""
    cur = conn.cursor()
    cur.execute(
        """SELECT DISTINCT d.platform, d.account FROM report_detail d
           WHERE d.report_id=? AND d.style_code=?""",
        (report_id, style_code),
    )
    rows = cur.fetchall()
    order = {"amazon": 0, "other": 1, "walmart": 2, "temu": 3}
    # 亚马逊账号按配置 sort_order
    cur.execute(
        "SELECT account_prefix, sort_order FROM style_accounts WHERE style_code=? AND platform='amazon'",
        (style_code,),
    )
    acc_order = {a: o for a, o in cur.fetchall()}
    rows.sort(key=lambda r: (order.get(r[0], 9), acc_order.get(r[1], 99)))
    out = []
    for platform, account in rows:
        label = PLATFORM_LABELS.get(platform, "{style}-" + platform).format(
            style=style_code, account=account or ""
        )
        out.append({"platform": platform, "account": account, "label": label})
    return out


def get_kpis(conn, report_id: int) -> Dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(DISTINCT sku), SUM(current_order_quantity), SUM(total_need) FROM report_summary WHERE report_id=?",
        (report_id,),
    )
    sku_count, total_order, total_need = cur.fetchone()
    cur.execute(
        "SELECT COUNT(DISTINCT sku) FROM report_summary WHERE report_id=? AND current_order_quantity > 0",
        (report_id,),
    )
    skus_to_order = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT style_code) FROM report_summary WHERE report_id=?", (report_id,))
    style_count = cur.fetchone()[0]
    return {
        "sku_count": sku_count or 0,
        "style_count": style_count or 0,
        "skus_to_order": skus_to_order or 0,
        "total_order_quantity": int(total_order or 0),
        "total_need": int(total_need or 0),
    }
