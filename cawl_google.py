#!/usr/bin/env python3
"""从 Google Sheets 读取数据，清洗后输出合并 CSV 并刷新 PostgreSQL。"""
import csv
import hashlib
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import gspread
import psycopg
from google.oauth2.service_account import Credentials

# 输入配置和输出文件都放在脚本所在目录，便于从任意工作目录运行。
BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'sheets.json'
CSV = BASE / 'ads_daily_merged.csv'
# 只申请读取 Google Sheets 的权限。
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
# 表格前 3 行是标题/表头，实际数据从第 4 行开始。
HEADER_ROW = 3
DATA_START_ROW = 4

# 表格的 69 个业务字段，顺序必须与 Google Sheet 的列顺序一致。
# 不同分区中含义相近或重复的字段使用不同名称，避免输出列名冲突。
FIELDS = [
'date','spend_mxn_total','spend_usd_total','actual_spend_usd',
'media','placement_type','ad_account_source','operator','self_or_outsource',
'channel','agency_account','account_spend_currency','ad_account_id',
'impressions','cpm','clicks','ctr','click_conversion_rate','installs','cpi_usd',
'panel_registrations','panel_registration_rate','panel_registration_cost',
'panel_payments','panel_payment_rate','panel_payment_cost_usd',
'level1_registrations','level1_first_depositors','level1_conversion_rate',
'level1_first_deposit_amount','level1_registration_cost_usd',
'level1_first_deposit_cost_usd','level1_first_deposit_arppu','level1_roas',
'first_registrations','first_depositors','first_deposit_conversion_rate',
'first_deposit_amount','first_registration_cost_usd','first_deposit_cost_usd',
'first_deposit_arppu','first_deposit_roas',
'total_deposit','total_depositors','total_withdrawal','net_deposit',
'daily_roi','total_deposit_arppu','total_deposit_roas',
'recharge_d1','recharge_d3','recharge_d7','recharge_d15','recharge_d30',
'ltv_d0','ltv_d3','ltv_d7','ltv_d15','ltv_d30',
'mxn_topup','mxn_spend','mxn_refund','mxn_fee','mxn_balance',
'usdt_topup','usdt_spend','usdt_refund','usdt_fee','usdt_balance'
]
# 这些列按文本保存，其余业务列按数字清洗。
TEXT_FIELDS = {'media','placement_type','ad_account_source','operator',
'self_or_outsource','channel','agency_account','account_spend_currency','ad_account_id'}
# 百分比字段清单供转换逻辑识别；只有原始单元格带 '%' 时才会除以 100。
PERCENT_FIELDS = {'ctr','click_conversion_rate','panel_registration_rate',
'panel_payment_rate','level1_conversion_rate','level1_roas',
'first_deposit_conversion_rate','first_deposit_roas','daily_roi',
'total_deposit_roas','recharge_d1','recharge_d3','recharge_d7',
'recharge_d15','recharge_d30'}


def clean_date(value):
    """把支持的日期格式统一为 YYYY-MM-DD；标题、汇总行或无法识别的值返回 None。"""
    value = str(value or '').strip()
    if not value or value in {'汇总','总计','合计','日期'}:
        return None
    for fmt in ('%Y/%m/%d','%Y-%m-%d','%Y.%m.%d','%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def clean_number(value, percent=False):
    """清理数字文本并返回十进制字符串；空值、表格错误值和无穷值返回 None。"""
    s = str(value if value is not None else '').strip()
    if s in ('', '-', '—', '–', 'N/A', '#DIV/0!', '#VALUE!'):
        return None
    s = s.replace(',', '').replace('，', '').replace(' ', '').replace('％','%')
    # Only strip a trailing stray letter after a valid percentage (e.g. 8.33%v).
    s = re.sub(r'(?<=%)\s*[A-Za-z]+$', '', s)
    is_pct = s.endswith('%')
    if is_pct:
        s = s[:-1]
    try:
        n = Decimal(s)
        if not n.is_finite():
            return None
        if is_pct:
            n /= 100  # 百分号数值转为小数比例，例如 3.82% -> 0.0382。
        return str(n)
    except InvalidOperation:
        logging.warning('Unrecognized numeric value: %r', value)
        return None


def row_to_record(values, source_id, tab, source_operator, row_num):
    """将工作表的一行转换为标准记录；日期无效的行（如表头/汇总行）会被跳过。"""
    # 短行补空值，超出字段定义的列会被截去，使每条记录与 FIELDS 对齐。
    values = (values + [''] * len(FIELDS))[:len(FIELDS)]
    day = clean_date(values[0])
    if not day:
        return None
    rec = {}
    for field, val in zip(FIELDS, values):
        if field == 'date':
            rec[field] = day
        elif field in TEXT_FIELDS:
            rec[field] = str(val or '').strip() or None
        else:
            # 传入百分比字段集合供分类使用；转换仍以单元格是否带 '%' 为准。
            rec[field] = clean_number(val, field in PERCENT_FIELDS)
    # 保存来源信息，便于追溯记录来自哪个表格、工作表和行。
    rec['source_spreadsheet_id'] = source_id
    rec['source_worksheet'] = tab
    rec['source_operator'] = source_operator
    rec['source_row'] = row_num
    # 用来源表格 ID 和行号生成稳定键，供刷新时替换同一来源数据；
    # 这不是按日期/账户等业务字段做重复记录合并。
    rec['record_key'] = hashlib.sha256(
        json.dumps([source_id,tab,row_num],ensure_ascii=False).encode()
    ).hexdigest()
    return rec


def upload_to_postgres(records, columns):
    """在 PostgreSQL 事务中清空目标表并写入本次全部记录。"""
    text_columns = TEXT_FIELDS | {
        'record_key', 'source_spreadsheet_id', 'source_worksheet', 'source_operator'
    }
    types = {
        column: ('TEXT' if column in text_columns else
                 'BIGINT' if column == 'source_row' else
                 'DATE' if column == 'date' else 'NUMERIC')
        for column in columns
    }
    definitions = ', '.join(
        f'"{column}" {types[column]}'
        + (' PRIMARY KEY' if column == 'record_key' else '')
        for column in columns
    )
    column_names = ', '.join(f'"{column}"' for column in columns)
    placeholders = ', '.join(['%s'] * len(columns))
    create_table = f'CREATE TABLE IF NOT EXISTS public.ads_daily_raw ({definitions})'
    insert_sql = (
        f'INSERT INTO public.ads_daily_raw ({column_names}) VALUES ({placeholders})'
    )
    rows = [
        tuple(date.fromisoformat(record['date']) if column == 'date'
              else record.get(column) for column in columns)
        for record in records
    ]

    with psycopg.connect(
        host='localhost', port=5432, dbname='postgres',
        user='postgres', password='147258',
    ) as connection:
        print('sssssss', connection)
        
        with connection.cursor() as cursor:
            cursor.execute(create_table)
            cursor.execute('DELETE FROM public.ads_daily_raw')
            cursor.executemany(insert_sql, rows)

    logging.info('PostgreSQL refreshed: public.ads_daily_raw (%d rows)', len(records))


def main():
    """读取配置和所有数据源；全部成功后刷新数据库并生成合并 CSV。"""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    sources = config['sources']
    if not sources:
        raise RuntimeError('No sources configured')
    # 使用服务账号凭据访问 Google Sheets，并限制为只读权限。
    creds = Credentials.from_service_account_file(
        str(BASE / config.get('credentials','service_account.json')), scopes=SCOPES)
    client = gspread.authorize(creds)
    all_records = []
    errors = []
    # 逐个读取配置中的表格；可指定一个工作表，也可读取该表格的全部工作表。
    for src in sources:
        sid = src['spreadsheet_id']
        name = src.get('operator',sid)
        try:
            book = client.open_by_key(sid)
            tabs = ([book.worksheet(src['worksheet'])] if src.get('worksheet')
                    else book.worksheets())
            count = 0
            for ws in tabs:
                rows = ws.get_all_values(value_render_option='FORMATTED_VALUE')
                time.sleep(2)
                if len(rows) < HEADER_ROW:
                    continue
                # 至少要包含脚本定义的字段数；表格多出的列会在 row_to_record 中忽略。
                if len(rows[HEADER_ROW - 1]) < len(FIELDS):
                    raise ValueError(f'{ws.title}: expected at least {len(FIELDS)} header columns, got {len(rows[HEADER_ROW-1])}')
                for row_no, vals in enumerate(rows[DATA_START_ROW-1:], DATA_START_ROW):
                    rec = row_to_record(vals,sid,ws.title,name,row_no)
                    if rec:
                        all_records.append(rec)
                        count += 1
            logging.info('%s: %s valid rows', name, count)
        except Exception as exc:
            errors.append(f'{name} ({sid}): {exc}')
            logging.exception('Source failed: %s', name)
    # 任意来源失败就停止，避免用不完整的数据刷新输出。
    if errors:
        logging.error('%s source(s) failed; outputs not updated:\n%s',len(errors),'\n'.join(errors))
        sys.exit(1)
    # 空结果不覆盖现有输出，通常表示配置或源数据有误。
    if not all_records:
        raise RuntimeError('No valid rows; refusing to overwrite existing outputs')
    # 显式定义 CSV 和 PostgreSQL 的字段顺序。
    meta = ['record_key','source_spreadsheet_id','source_worksheet','source_operator','source_row']
    columns = meta + FIELDS
    # 以 UTF-8 BOM 编码写 CSV，方便 Excel 识别中文。
    with CSV.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        writer.writerows(all_records)
    # 将相同的完整数据集同步到 PostgreSQL；数据库连接信息通过环境变量提供。
    upload_to_postgres(all_records, columns)
    logging.info('DONE: %d rows; PostgreSQL table=public.ads_daily_raw; csv=%s',len(all_records),CSV)


if __name__ == '__main__':
    main()
