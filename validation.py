from __future__ import annotations

from collections import Counter


COUNT_FIELDS = ('impressions', 'clicks', 'installs', 'registrations',
                'level1_depositors', 'first_depositors')
NONNEGATIVE_FIELDS = ('spend',) + COUNT_FIELDS + ('level1_amount',)


def duplicate_counts(records: list[dict]) -> Counter:
    counts = Counter()
    for record in records:
        if record.get('data_type') not in {'yesterday', 'daily'}:
            continue
        if record.get('date') is None:
            continue
        key = (record.get('group'), record.get('line_id'), record['date'])
        counts[key] += 1
    return counts


def validate_record(record: dict, is_duplicate: bool = False) -> tuple[list[str], list[str]]:
    severe = []
    warnings = []
    for field in ('operator', 'group', 'media', 'system', 'spend',
                  'level1_depositors', 'level1_amount'):
        if record.get(field) in (None, ''):
            severe.append(f'缺少必填字段：{field}')
    if record.get('data_type') == 'yesterday' and record.get('date') is None:
        severe.append('昨日记录缺少有效日期')
    if is_duplicate:
        severe.append('同一投放线同一日期存在重复记录')

    for field in NONNEGATIVE_FIELDS:
        value = record.get(field)
        if value is not None and value < 0:
            severe.append(f'{field}出现非法负数：{value:g}')

    zero_denominators = (
        ('impressions', 'clicks', '曝光量为0但点击量大于0'),
        ('clicks', 'installs', '点击量为0但安装量大于0'),
        ('registrations', 'level1_depositors', '一级注册人数为0但一级首充人数大于0'),
        ('spend', 'level1_depositors', '消耗为0但一级首充人数大于0'),
        ('spend', 'level1_amount', '消耗为0但一级首充金额大于0'),
    )
    for denominator, numerator, message in zero_denominators:
        if record.get(denominator) == 0 and (record.get(numerator) or 0) > 0:
            severe.append(message)

    ratios = (
        ('reported_cpa', 'spend', 'level1_depositors', '一级首充成本$'),
        ('reported_roas', 'level1_amount', 'spend', '一级ROAS'),
        ('reported_arppu', 'level1_amount', 'level1_depositors', '一级ARPPU$'),
        ('reported_cpc', 'spend', 'clicks', 'CPC$'),
        ('reported_click_install', 'installs', 'clicks', '点击安装率'),
        ('reported_install_registration', 'registrations', 'installs', '安装注册率'),
        ('reported_registration_deposit', 'level1_depositors', 'registrations', '一级注册首充率'),
    )
    for reported, numerator, denominator, label in ratios:
        value = record.get(reported)
        n, d = record.get(numerator), record.get(denominator)
        if value is None or n is None or not d:
            continue
        computed = n / d
        tolerance = max(0.02, abs(computed) * 0.03)
        if abs(value - computed) > tolerance:
            warnings.append(
                f'{label}与原始分子/分母计算不一致（报表{value:.4g}，计算{computed:.4g}）'
            )
    return severe, warnings
