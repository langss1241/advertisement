from __future__ import annotations

from datetime import date
from pathlib import Path
import re

import pandas as pd


FIELD_ALIASES = {
    'data_type': ('数据类型', 'data_type', 'record_type'),
    'date': ('日期', 'date', 'day'),
    'operator': ('投手', 'operator', 'source_operator'),
    'group': ('组别', 'group', 'group_name', 'operator_group'),
    'media': ('媒体', 'media', 'source_media'),
    'system': ('系统', 'system', 'os_type', 'placement_type'),
    'line_id': ('投放线ID', '投放线', 'line_id', '渠道', 'channel'),
    'spend': ('实际消耗$', '实际消耗', 'actual_spend_usd', 'spend'),
    'level1_depositors': ('一级首充人数', 'level1_first_depositors', 'level1_depositors'),
    'first_depositors': ('首充人数', '总首充人数', 'first_depositors'),
    'level1_amount': ('一级首充金额$', '一级首充金额', 'level1_first_deposit_amount', 'level1_amount'),
    'impressions': ('曝光量', 'impressions'),
    'clicks': ('点击量', 'clicks'),
    'installs': ('安装量', 'installs'),
    'registrations': ('一级注册人数', '一级注册人数', 'level1_registrations', 'registrations'),
    'recharge_d1': ('次日复充率', 'recharge_d1'),
    'reported_cpa': ('一级首充成本$', '一级首充成本', 'reported_cpa'),
    'cost_deviation': (
        '成本偏离阈值%', '成本偏离阀值%', '超成本阈值', '超成本阀值',
        '超成本阈值%', '超成本阀值%', 'cost_deviation',
    ),
    'reported_roas': ('一级ROAS', 'level1_roas', 'reported_roas'),
    'reported_arppu': ('一级ARPPU$', '一级ARPPU', 'level1_first_deposit_arppu', 'reported_arppu'),
    'reported_cpc': ('CPC$', 'CPC', 'cpc'),
    'reported_click_install': ('点击安装率', 'click_install_rate'),
    'reported_install_registration': ('安装注册率', 'install_registration_rate'),
    'reported_registration_deposit': ('一级注册首充率', 'registration_deposit_rate'),
    'manual_grade': ('人工最终分级', '人工分级', '分级', 'manual_grade'),
    'manual_reason': ('人工修正原因', '结论', 'manual_reason'),
}

NUMERIC_FIELDS = {
    'spend', 'level1_depositors', 'first_depositors', 'level1_amount',
    'impressions', 'clicks', 'installs', 'registrations', 'recharge_d1',
    'reported_cpa', 'reported_roas', 'reported_arppu', 'reported_cpc',
    'cost_deviation',
    'reported_click_install', 'reported_install_registration',
    'reported_registration_deposit',
}

REPORT_TYPES = {
    '昨日': 'yesterday',
    '前7日明细': 'daily',
    '前7日均值': 'average',
    'yesterday': 'yesterday',
    'daily': 'daily',
    'average': 'average',
}


def _find_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    by_normalized = {str(column).strip().casefold(): str(column) for column in columns}
    return next((by_normalized[name.casefold()] for name in aliases
                 if name.casefold() in by_normalized), None)


def _parse_date(value: object) -> date | None:
    if value is None or pd.isna(value) or not str(value).strip():
        return None
    parsed = pd.to_datetime(value, errors='coerce')
    return None if pd.isna(parsed) else parsed.date()


def _number(value: object) -> float | None:
    if value is None or pd.isna(value) or not str(value).strip():
        return None
    parsed = pd.to_numeric(value, errors='coerce')
    return None if pd.isna(parsed) else float(parsed)


def _group(value: object) -> str:
    text = '' if value is None or pd.isna(value) else str(value).strip()
    match = re.fullmatch(r'([ABC])(?:组)?', text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else text


def load_frame(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if source.suffix.casefold() == '.csv':
        return pd.read_csv(source, encoding='utf-8-sig')
    if source.suffix.casefold() in {'.xlsx', '.xlsm'}:
        return pd.read_excel(source)
    raise ValueError(f'Unsupported input format: {source.suffix}; use CSV or XLSX')


def normalize_frame(frame: pd.DataFrame) -> tuple[list[dict], list[str]]:
    columns = list(frame.columns)
    mapping = {
        field: _find_column(columns, aliases)
        for field, aliases in FIELD_ALIASES.items()
    }
    required = ('operator', 'group', 'media', 'system', 'spend',
                'level1_depositors', 'level1_amount')
    missing = [field for field in required if mapping[field] is None]
    if missing:
        raise ValueError(
            'Input is missing required fields: ' + ', '.join(missing)
            + '. Raw source CSVs need a group mapping and line-level report fields.'
        )

    raw_records = []
    for _, row in frame.iterrows():
        record = {}
        for field, column in mapping.items():
            value = row[column] if column is not None else None
            if field in NUMERIC_FIELDS:
                record[field] = _number(value)
            elif field == 'date':
                record[field] = _parse_date(value)
            elif field == 'group':
                record[field] = _group(value)
            elif field == 'data_type':
                text = '' if value is None or pd.isna(value) else str(value).strip()
                record[field] = REPORT_TYPES.get(text, text)
            else:
                record[field] = None if value is None or pd.isna(value) else str(value).strip()

        for field in ('operator', 'media', 'system', 'group', 'line_id'):
            if record.get(field) is None:
                record[field] = ''
        if not record['line_id']:
            record['line_id'] = '|'.join(
                (record['group'], record['operator'], record['media'], record['system'])
            )
            record['line_id_source'] = 'composite'
        else:
            record['line_id_source'] = 'provided'
        record['manual_grade'] = (
            str(record['manual_grade']).strip().upper()
            if record['manual_grade'] else None
        )
        record['manual_reason'] = record['manual_reason'] or None
        raw_records.append(record)

    dated = [record['date'] for record in raw_records if record['date']]
    report_date = max(dated) if dated else None
    for record in raw_records:
        if not record['data_type']:
            record['data_type'] = (
                'yesterday' if record['date'] == report_date else 'daily'
            )
    return raw_records, [field for field in required if mapping[field] is None]


def load_report(path: str | Path) -> tuple[list[dict], list[str]]:
    return normalize_frame(load_frame(path))
