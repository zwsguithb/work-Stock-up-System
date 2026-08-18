import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from . import database as db

HOST = os.getenv("LINGXING_HOST", "https://openapi.lingxing.com")
APP_ID = os.getenv("LINGXING_APP_ID", "")
APP_SECRET = os.getenv("LINGXING_APP_SECRET", "")

FBA_STOCK_PATH = os.getenv("LINGXING_FBA_STOCK_PATH", "/basicOpen/openapi/storage/fbaWarehouseDetail")
FULL_STOCK_PATH = os.getenv("LINGXING_FULL_STOCK_PATH", "/basicOpen/multiplatform/full/stockSearch")
FBT_STOCK_PATH = os.getenv("LINGXING_FBT_STOCK_PATH", "/basicOpen/multiplatform/fbt/stockSearch")
DAILY_SALES_PATH = os.getenv("LINGXING_DAILY_SALES_PATH", "/basicOpen/platformStatisticsV2/saleStat/pageList")


def _get_config(conn) -> Dict:
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM system_config")
    cfg = {k: v for k, v in cur.fetchall()}
    return cfg


def _get_token(conn) -> Optional[str]:
    cfg = _get_config(conn)
    app_id = cfg.get("lingxing_app_id") or APP_ID
    app_secret = cfg.get("lingxing_app_secret") or APP_SECRET
    if not app_id or not app_secret:
        return None
    url = f"{HOST}/api/auth-server/oauth/access-token"
    try:
        r = requests.post(url, data={"appId": app_id, "appSecret": app_secret}, timeout=30)
        data = r.json()
        if data.get("code") != 0:
            print("lingxing auth error:", data)
            return None
        return data.get("data", {}).get("access_token")
    except Exception as e:
        print("lingxing auth exception:", e)
        return None


def _list_all(path: str, token: str, params: Dict) -> List[Dict]:
    """分页拉取，兼容 data / data.list / data.records / data[]"""
    items = []
    page = 1
    size = 500
    while True:
        p = dict(params)
        p.update({"page": page, "pageSize": size, "access_token": token})
        try:
            r = requests.post(f"{HOST}{path}", data=p, timeout=60)
            data = r.json()
        except Exception as e:
            print("lingxing request exception", path, e)
            break
        if data.get("code") != 0:
            print("lingxing api error", path, data)
            break
        payload = data.get("data", {})
        if isinstance(payload, list):
            page_items = payload
        elif isinstance(payload, dict):
            page_items = payload.get("list") or payload.get("records") or []
        else:
            page_items = []
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < size:
            break
        page += 1
    return items


def fetch_daily_sales(token: str, sid: str, start_date: str, end_date: str) -> List[Dict]:
    """拉取某店铺某日销量，按 SKU 汇总"""
    # date_unit=4 按日, data_type=4 SKU, result_type=1 汇总
    items = _list_all(
        DAILY_SALES_PATH,
        token,
        {
            "sid": sid,
            "start_date": start_date,
            "end_date": end_date,
            "date_unit": "4",
            "data_type": "4",
            "result_type": "1",
        },
    )
    return items


def fetch_fba_stock(token: str, sid: str) -> List[Dict]:
    return _list_all(FBA_STOCK_PATH, token, {"sid": sid})


def fetch_full_stock(token: str, sids: str) -> List[Dict]:
    # selectTypeEnum=COUNT_TYPE 获取库存数量
    return _list_all(FULL_STOCK_PATH, token, {"sid": sids, "selectTypeEnum": "COUNT_TYPE"})


def fetch_fbt_stock(token: str, store_ids: str) -> List[Dict]:
    return _list_all(FBT_STOCK_PATH, token, {"storeIdList": store_ids})


def sync_from_lingxing(conn, baseline_date: str) -> Dict:
    token = _get_token(conn)
    if not token:
        return {"status": "error", "message": "未配置领星 AppId/AppSecret"}

    cfg = _get_config(conn)

    # 账号前缀 -> sid 列表
    amazon_sids: Dict[str, List[str]] = {}
    for key, val in cfg.items():
        if key.startswith("lingxing_sids_") and val:
            prefix = key.replace("lingxing_sids_", "").upper()
            amazon_sids[prefix] = [s.strip() for s in str(val).split(",") if s.strip()]

    other_sids = [s.strip() for s in str(cfg.get("lingxing_sids_other") or "").split(",") if s.strip()]
    walmart_sids = [s.strip() for s in str(cfg.get("lingxing_sids_walmart") or "").split(",") if s.strip()]
    temu_store_ids = [s.strip() for s in str(cfg.get("lingxing_sids_temu") or "").split(",") if s.strip()]

    base = datetime.strptime(baseline_date, "%Y-%m-%d")
    end = base - timedelta(days=1)
    start = end - timedelta(days=59)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    cur = conn.cursor()
    cur.execute("DELETE FROM sales_raw")
    cur.execute("DELETE FROM inventory_raw")

    sales_count = 0
    inv_count = 0
    logs = []

    # 亚马逊销量 + FBA 库存
    for prefix, sids in amazon_sids.items():
        for sid in sids:
            try:
                items = fetch_daily_sales(token, sid, start_str, end_str)
                for it in items:
                    sku = it.get("sku") or it.get("product_sku") or it.get("seller_sku")
                    qty = it.get("qty") or it.get("quantity") or it.get("sale_qty") or 0
                    date = it.get("date") or it.get("biz_date") or ""
                    if not sku or not date:
                        continue
                    date = str(date).replace("-", "")
                    cur.execute(
                        "INSERT INTO sales_raw (date, sku, platform, account, quantity, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (date, sku, "amazon", prefix, float(qty), db.now()),
                    )
                    sales_count += 1
            except Exception as e:
                logs.append(f"amazon sales {prefix}/{sid}: {e}")
            try:
                invs = fetch_fba_stock(token, sid)
                for it in invs:
                    sku = it.get("sku") or it.get("product_sku") or it.get("seller_sku")
                    qty = it.get("qty") or it.get("quantity") or it.get("fulfillable_quantity") or 0
                    if not sku:
                        continue
                    cur.execute(
                        "INSERT INTO inventory_raw (sku, platform, account, warehouse, quantity, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (sku, "amazon", prefix, "FBA", float(qty), db.now()),
                    )
                    inv_count += 1
            except Exception as e:
                logs.append(f"amazon fba {prefix}/{sid}: {e}")

    # 其他平台 / 中国仓 / 沃尔玛：FULL 接口
    all_full_sids = ",".join(other_sids + walmart_sids)
    if all_full_sids:
        try:
            invs = fetch_full_stock(token, all_full_sids)
            for it in invs:
                sku = it.get("sku") or it.get("productSku") or it.get("sellerSku")
                qty = it.get("qty") or it.get("quantity") or it.get("availableQty") or 0
                wh = it.get("warehouseName") or it.get("warehouse_name") or ""
                if not sku:
                    continue
                # 根据仓库名或店铺归属判断平台
                platform = "other"
                account = None
                if "沃尔玛" in wh or "walmart" in wh.lower():
                    platform = "walmart"
                elif "中国仓" in wh or "国内" in wh or "CN" in wh.upper():
                    platform = "other"
                    account = "中国仓"
                cur.execute(
                    "INSERT INTO inventory_raw (sku, platform, account, warehouse, quantity, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (sku, platform, account, wh, float(qty), db.now()),
                )
                inv_count += 1
        except Exception as e:
            logs.append(f"full stock: {e}")

    # Temu 库存
    if temu_store_ids:
        try:
            invs = fetch_fbt_stock(token, ",".join(temu_store_ids))
            for it in invs:
                sku = it.get("sku") or it.get("productSku") or it.get("sellerSku")
                qty = it.get("qty") or it.get("quantity") or it.get("availableQty") or 0
                if not sku:
                    continue
                cur.execute(
                    "INSERT INTO inventory_raw (sku, platform, account, warehouse, quantity, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (sku, "temu", None, "FBT", float(qty), db.now()),
                )
                inv_count += 1
        except Exception as e:
            logs.append(f"fbt stock: {e}")

    # 其他平台 / 沃尔玛 / temu 销量：日销量接口按 sid 拉
    def pull_platform_sales(sids: List[str], platform: str):
        nonlocal sales_count
        for sid in sids:
            try:
                items = fetch_daily_sales(token, sid, start_str, end_str)
                for it in items:
                    sku = it.get("sku") or it.get("product_sku") or it.get("seller_sku")
                    qty = it.get("qty") or it.get("quantity") or it.get("sale_qty") or 0
                    date = it.get("date") or it.get("biz_date") or ""
                    if not sku or not date:
                        continue
                    date = str(date).replace("-", "")
                    cur.execute(
                        "INSERT INTO sales_raw (date, sku, platform, account, quantity, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (date, sku, platform, None, float(qty), db.now()),
                    )
                    sales_count += 1
            except Exception as e:
                logs.append(f"{platform} sales {sid}: {e}")

    pull_platform_sales(other_sids, "other")
    pull_platform_sales(walmart_sids, "walmart")
    pull_platform_sales(temu_store_ids, "temu")

    conn.commit()
    return {
        "status": "ok",
        "sales_rows": sales_count,
        "inventory_rows": inv_count,
        "logs": logs,
    }
