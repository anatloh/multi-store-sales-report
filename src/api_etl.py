import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from tqdm import tqdm
from decouple import config
from playwright.sync_api import sync_playwright
import pyodbc

# --- CONFIGURATION (Loaded safely from environment variables) ---
DB_CONN = (
    f"Driver={{ODBC Driver 17 for SQL Server}};"
    f"Server={config('DB_SERVER', default='localhost,1433')};"
    f"Database={config('DB_DATABASE', default='SalesDataDB')};"
    f"Trusted_Connection={config('DB_TRUSTED_CONN', default='yes')};"
    "Connection Timeout=30;"
    "Pooling=False;"
)
AUTH_JSON_PATH = os.path.join(os.path.dirname(__file__), "auth.json")

# Portal Credentials
PORTAL_USERNAME = config("PORTAL_USERNAME", default="your_username")
PORTAL_PASSWORD = config("PORTAL_PASSWORD", default="your_password")
PORTAL_LOGIN_URL = config("PORTAL_LOGIN_URL", default="https://example.com/Account/LogOn")
PORTAL_SWITCH_URL = config("PORTAL_SWITCH_URL", default="https://example.com/CommonTools/SwitchAccount?accountId=00000")

# --- SHOP CLASSIFICATIONS (Example placeholders; load dynamically from DB if needed) ---
DIRECT_IDS = config("DIRECT_SHOP_IDS", default="", cast=lambda v: [s.strip() for s in v.split(",") if s.strip()])
FRANCHISE_IDS = config("FRANCHISE_SHOP_IDS", default="", cast=lambda v: [s.strip() for s in v.split(",") if s.strip()])

def get_db_connection(retries=3, delay=5):
    """Obtains a fresh SQL connection with retry logic."""
    for attempt in range(1, retries + 1):
        try:
            return pyodbc.connect(DB_CONN)
        except pyodbc.Error as e:
            print(f"!!! [DB CONNECT FAILED] Attempt {attempt} of {retries}: {e}")
            if attempt < retries:
                time.sleep(delay)
            else:
                raise e

def clear_existing_data(dates_to_sync):
    """Cleans up itemized tables prior to full re-sync."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCK_TIMEOUT 5000;") 
                for d in dates_to_sync:
                    print(f"--- [CLEANUP] Removing records for {d} ---")
                    cursor.execute("DELETE FROM dbo.Sales_Header WHERE txDate = ?", (d,))
                    cursor.execute("DELETE FROM dbo.DailyItemSales WHERE txDate = ?", (d,))
            conn.commit()
    except Exception as e:
        print(f"!!! Error during cleanup: {e}")

def get_shop_map():
    """Fetches shop list from Shop_Master."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT shopId FROM dbo.Shop_Master")
                return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"!!! Error loading shop map: {e}")
        return []

def login_and_setup(max_retries=3, delay_seconds=5):
    """Authenticates via Playwright and refreshes auth.json."""
    target_dir = os.path.dirname(AUTH_JSON_PATH)
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir)

    for attempt in range(1, max_retries + 1):
        print(f"--- [LOGIN] Attempt {attempt} of {max_retries} ---")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.set_default_timeout(30000)

            try:
                page.goto(PORTAL_LOGIN_URL, wait_until="domcontentloaded")
                page.fill("input[name='UserName']", PORTAL_USERNAME)
                page.fill("input[name='Password']", PORTAL_PASSWORD)
                page.click("button[type='submit']")
                page.wait_for_url(lambda url: "LogOn" not in url, timeout=15000)
                
                page.goto(PORTAL_SWITCH_URL, wait_until="domcontentloaded")
                context.storage_state(path=AUTH_JSON_PATH)
                print("--- [LOGIN SUCCESS] ---")
                browser.close()
                return True

            except Exception as e:
                print(f"!!! [LOGIN FAILED] Attempt {attempt}: {e}")
                browser.close()
                if attempt < max_retries:
                    time.sleep(delay_seconds * attempt)
                else:
                    raise e

def fetch_group_sales_total(request_context, date_str, shop_ids, headers, start_hour=5, end_hour=23):
    """Calculates hourly sales total for a group of shop IDs."""
    if not shop_ids:
        return 0.0

    url = "https://example.com/ReportHourlySales/ReportHourlySales_Read"
    payload = [
        ('selectedDateFrom', date_str),
        ('selectedDateTo', date_str),
        ('pageSize', '5000')
    ]
    for s_id in shop_ids:
        payload.append(('selectedShopIdList', str(s_id)))

    try:
        resp = request_context.post(url, data=urlencode(payload, doseq=True), headers=headers)
        if resp.status == 200:
            data = resp.json().get("Data", [])
            total = 0.0
            for entry in data:
                hour = int(entry.get("HourInDay", -1))
                if start_hour <= hour <= end_hour:
                    total += float(entry.get("NetAmount") or 0.0)
            return round(total, 2)
    except Exception as e:
        print(f"!!! Error calculating hourly sales: {e}")
    return 0.0

def save_to_daily_api_sales(sql_date_str, direct, franchise, grand):
    """MERGE SQL logic to safely update/insert into Daily_API_Sales."""
    merge_sql = """
    MERGE INTO dbo.Daily_API_Sales AS target
    USING (SELECT ? AS ReportDate) AS source
    ON target.ReportDate = source.ReportDate
    WHEN MATCHED THEN
        UPDATE SET DirectSales = ?, FranchiseSales = ?, GrandTotal = ?
    WHEN NOT MATCHED THEN
        INSERT (ReportDate, DirectSales, FranchiseSales, GrandTotal)
        VALUES (?, ?, ?, ?);
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    merge_sql, 
                    (sql_date_str, direct, franchise, grand, sql_date_str, direct, franchise, grand)
                )
            conn.commit()
        print(f"--- [API SALES RECORDED] Date: {sql_date_str} | Direct: {direct:,.2f} | Franchise: {franchise:,.2f} | Grand: {grand:,.2f} ---")
    except Exception as e:
        print(f"!!! SQL Error in save_to_daily_api_sales: {e}")

def get_individual_transactions(request_context, date_str, shop_ids, headers):
    """Fetches raw transaction list rows."""
    try:
        query_params = [
            ('selectedDateFrom', date_str),
            ('selectedDateTo', date_str),
            ('isUseStartOrderTimeToDetermineTxDaySection', 'False')
        ]
        for s_id in shop_ids:
            query_params.append(('selectedShopIdList', str(s_id)))

        url = f"https://example.com/ReportTransactionList/ReportTransactionList_Read?{urlencode(query_params, doseq=True)}"
        resp = request_context.post(url, data="sort=&group=&filter=", headers=headers)
        if resp.status != 200:
            return []

        res_json = resp.json()
        if isinstance(res_json, dict):
            return res_json.get("Data") or res_json.get("DataList") or res_json.get("Result") or []
        elif isinstance(res_json, list):
            return res_json
        return []
    except Exception as e:
        print(f"!!! Error parsing transaction arrays: {e}")
        return []

def upsert_sales_items(data_list):
    """Bulk inserts item breakdowns into DailyItemSales."""
    if not data_list: return
    sql = "INSERT INTO dbo.DailyItemSales (txDate, shopId, itemName, quantity, amount) VALUES (?, ?, ?, ?, ?)"
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.fast_executemany = True
            cursor.executemany(sql, data_list)
        conn.commit()

def process_single_shop(s_id, date_str, original_date, headers, request_context):
    """Pagination loop for menu item breakdown per shop."""
    items_for_shop = []
    page_number = 1
    previous_data_hash = None
    
    while page_number < 50:
        form_data = [
            ('selectedDateFrom', date_str), ('selectedDateTo', date_str),
            ('selectedShopIdList', str(s_id)), ('pageSize', '500'), ('page', str(page_number))
        ]
        try:
            resp = request_context.post(
                "https://example.com/ReportSalesByItem/ReportSalesByItem_Read",
                data=urlencode(form_data, doseq=True), 
                headers=headers, timeout=30000 
            )
            if resp.status != 200: break
                
            data = resp.json()
            items = data.get('Data', [])
            if not items: break

            current_data_hash = str(items[0]) if items else ""
            if current_data_hash == previous_data_hash: break
            previous_data_hash = current_data_hash
            
            for item in items:
                p_name = item.get('ItemName') or item.get('ProductName')
                qty = float(item.get('TotalQty') or item.get('Qty') or 0.0)
                amt = float(item.get('TotalAmount') or item.get('Amount') or 0.0)
                
                if p_name:
                    items_for_shop.append((original_date, s_id, p_name, qty, amt))
            page_number += 1
        except Exception:
            break
            
    return items_for_shop

def sync_all_shops(all_shop_ids, dates_to_sync, headers):
    """Syncs transaction headers and menu item sales."""
    formatted_dates = [datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y") for d in dates_to_sync]
    now = datetime.now()
    current_time_obj = now.time()
    
    with sync_playwright() as p:
        request_context = p.request.new_context(storage_state=AUTH_JSON_PATH, timeout=120000)
        
        for date_str, original_date in zip(formatted_dates, dates_to_sync):
            print(f"\n--- PROCESSING ITEM DETAILED SYNC FOR DATE: {original_date} ---")
            is_last_week = (datetime.strptime(original_date, "%Y-%m-%d").date() == (datetime.now() - timedelta(days=7)).date())
            
            raw_tx_list = get_individual_transactions(request_context, date_str, all_shop_ids, headers)
            insert_batch = []
            seen_keys = set()
            
            for tx in raw_tx_list:
                raw_time = tx.get('CheckoutDatetime') or tx.get('TxDateTime') or tx.get('CreateDateTime') or tx.get('TxTime')
                try: 
                    real_dt = datetime.strptime(str(raw_time).replace('T', ' ').split('.')[0], "%Y-%m-%d %H:%M:%S")
                except: 
                    real_dt = datetime.now()
                
                if is_last_week and real_dt.time() > current_time_obj: 
                    continue
                
                raw_invoice = tx.get('TxNo') or tx.get('ReceiptNo') or tx.get('InvoiceNo') or tx.get('TxSalesHeaderId') or "0"
                order_no = int(''.join(filter(str.isdigit, str(raw_invoice))) or 0)
                shop_id = int(tx.get('ShopId') or tx.get('ShopID') or tx.get('BranchId') or 0)
                unique_key = (original_date, order_no, shop_id)
                
                if unique_key not in seen_keys and order_no != 0:
                    seen_keys.add(unique_key)
                    net_sales = float(tx.get('NetSalesAmount') or tx.get('NetAmount') or tx.get('AmountTotal') or 0.0)
                    insert_batch.append((order_no, original_date, net_sales, real_dt, shop_id))

            if insert_batch:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.fast_executemany = True
                        cursor.executemany("INSERT INTO dbo.Sales_Header (orderNo, txDate, amountTotal, checkoutDatetime, shopId) VALUES (?, ?, ?, ?, ?)", insert_batch)
                    conn.commit()

            print(f"Fetching item breakdowns for {len(all_shop_ids)} shops:")
            all_items = []
            
            for s_id in tqdm(all_shop_ids, desc="Syncing Shops", unit="shop"):
                items = process_single_shop(s_id, date_str, original_date, headers, request_context)
                if items:
                    all_items.extend(items)
                time.sleep(0.05) 
            
            if all_items:
                upsert_sales_items(all_items)
                print(f"Successfully saved {len(all_items)} menu items for {original_date}")
        
        request_context.dispose()

def run_sync():
    print(f"[{datetime.now()}] --- Starting Sync ---")
    today = datetime.now()
    dates = [today.strftime("%Y-%m-%d"), (today - timedelta(days=7)).strftime("%Y-%m-%d")]
    today_str_api = today.strftime("%d/%m/%Y")
    today_str_sql = today.strftime("%Y-%m-%d")
    headers = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    with sync_playwright() as p:
        request_context = p.request.new_context(storage_state=AUTH_JSON_PATH)
        print("Calculating exact total sales via ReportHourlySales...")
        direct_total = fetch_group_sales_total(request_context, today_str_api, DIRECT_IDS, headers, 5, 23)
        franchise_total = fetch_group_sales_total(request_context, today_str_api, FRANCHISE_IDS, headers, 5, 23)
        grand_total = round(direct_total + franchise_total, 2)
        
        save_to_daily_api_sales(today_str_sql, direct_total, franchise_total, grand_total)
        request_context.dispose()

    clear_existing_data(dates)
    all_shop_ids = get_shop_map()
    if not all_shop_ids:
        print("No shop IDs found in Shop_Master. Item sync aborted.")
        return

    sync_all_shops(all_shop_ids, dates, headers)
    print(f"[{datetime.now()}] --- Sync Completed Successfully ---")

if __name__ == "__main__":
    print("Running initial sync...")
    try: 
        login_and_setup() 
        run_sync()
    except Exception as e: 
        print(f"!!! [FATAL ERROR] {e}")