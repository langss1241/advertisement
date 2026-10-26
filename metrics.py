from __future__ import annotations

from datetime import timedelta
from collections import defaultdict

from validation import duplicate_counts, validate_record


SUM_FIELDS = (
    'spend', 'level1_depositors', 'first_depositors', 'level1_amount',
    'impressions', 'clicks', 'installs', 'registrations',
)
RATIO_FIELDS = (
    'cpa', 'roas', 'arppu', 'cpc', 'click_install_rate',
    'install_registration_rate', 'registration_deposit_rate',
)


def _sum(records: list[dict], field: str) -> float | None:
    values = [record.get(field) for record in records]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def derive_metrics(values: dict) -> dict:
    spend = values.get('spend')
    depositors = values.get('level1_depositors')
    amount = values.get('level1_amount')
    clicks = values.get('clicks')
    installs = values.get('installs')
    registrations = values.get('registrations')
    return {
        'cpa': spend / depositors if spend is not None and depositors else None,
        'roas': amount / spend if amount is not None and spend else None,
        'arppu': amount / depositors if amount is not None and depositors else None,
        'cpc': spend / clicks if spend is not None and clicks else None,
        'click_install_rate': installs / clicks if installs is not None and clicks else None,
        'install_registration_rate': (
            registrations / installs if registrations is not None and installs else None
        ),
        'registration_deposit_rate': (
            depositors / registrations
            if depositors is not None and registrations else None
        ),
    }


def _line_key(record: dict) -> tuple:
    return (
        record.get('group'), record.get('line_id'), record.get('operator'),
        record.get('media'), record.get('system'),
    )


def _retention_mean(records: list[dict]) -> float | None:
    values = [record['recharge_d1'] for record in records
              if record.get('recharge_d1') is not None]
    return sum(values) / len(values) if values else None


def _line_persistence(details: list[dict], target_date, thresholds: dict, system: str) -> int:
    cpa_limit = thresholds.get('cpa_usd', {}).get(system)
    roas_limit = thresholds.get('roas', {}).get(system)
    bad_dates = set()
    for record in details:
        if record.get('date') is None:
            continue
        metrics = derive_metrics(record)
        if ((cpa_limit is not None and metrics['cpa'] is not None and metrics['cpa'] > cpa_limit)
                or (roas_limit is not None and metrics['roas'] is not None
                    and metrics['roas'] < roas_limit)):
            bad_dates.add(record['date'])
    streak = 0
    check = target_date
    while check in bad_dates:
        streak += 1
        check -= timedelta(days=1)
    return streak


def build_assessments(
    records: list[dict], config: dict, requested_date=None
) -> tuple[list[dict], list[dict]]:
    eligible = set(config.get('eligible_groups', ['A', 'B', 'C']))
    eligible_records = [record for record in records if record.get('group') in eligible]
    duplicate_map = duplicate_counts(eligible_records)
    by_line = defaultdict(list)
    for record in eligible_records:
        by_line[_line_key(record)].append(record)

    explicit_dates = [
        record['date'] for record in eligible_records
        if record.get('data_type') == 'yesterday' and record.get('date')
    ]
    if not explicit_dates:
        explicit_dates = [record['date'] for record in eligible_records
                          if record.get('date') is not None]
    if not explicit_dates:
        return [], eligible_records
    target_date = requested_date or max(explicit_dates)
    baseline_dates = {target_date - timedelta(days=offset) for offset in range(1, 8)}
    assessments = []
    duplicate_keys = {key for key, count in duplicate_map.items() if count > 1}

    for key, line_records in by_line.items():
        current = [record for record in line_records
                   if record.get('data_type') == 'yesterday'
                   and record.get('date') == target_date]
        if not current:
            continue
        record = current[0]
        detail = [item for item in line_records
                  if item.get('data_type') == 'daily'
                  and item.get('date') in baseline_dates]
        average = [item for item in line_records if item.get('data_type') == 'average']
        issues, warnings = validate_record(
            record, (record.get('group'), record.get('line_id'), target_date) in duplicate_keys
        )
        for item in detail:
            detail_issues, detail_warnings = validate_record(
                item, (item.get('group'), item.get('line_id'), item.get('date')) in duplicate_keys
            )
            issues.extend(f'前7日数据：{message}' for message in detail_issues)
            warnings.extend(f'前7日数据：{message}' for message in detail_warnings)

        current_values = {field: record.get(field) for field in SUM_FIELDS}
        current_metrics = derive_metrics(current_values)
        baseline_source = '前7日明细'
        if detail:
            observed_dates = {item['date'] for item in detail if item.get('date')}
            baseline_values = {
                field: _sum(detail, field)
                for field in SUM_FIELDS
            }
            baseline_metrics = derive_metrics(baseline_values)
            baseline_days = len(observed_dates)
            baseline_complete = observed_dates == baseline_dates
            baseline_retention = _retention_mean(detail)
            if not baseline_complete:
                warnings.append(
                    f'前7日明细不完整：{baseline_days}/7个自然日，基线可信度不足'
                )
        elif average:
            baseline_source = '前7日均值（来源回退）'
            baseline_values = {field: average[0].get(field) for field in SUM_FIELDS}
            baseline_metrics = derive_metrics(baseline_values)
            baseline_days = 0
            baseline_complete = False
            baseline_retention = average[0].get('recharge_d1')
            warnings.append('未提供前7日明细，使用报表前7日均值；基线可信度较低')
        else:
            baseline_source = '缺失'
            baseline_values = {field: None for field in SUM_FIELDS}
            baseline_metrics = {field: None for field in RATIO_FIELDS}
            baseline_days = 0
            baseline_complete = False
            baseline_retention = None
            warnings.append('缺少前7日明细及均值')

        baseline_validation = {
            'baseline_source': baseline_source,
            'baseline_days': baseline_days,
            'baseline_complete': baseline_complete,
            'baseline_values': baseline_values,
            'baseline': baseline_metrics,
            'baseline_recharge_d1': baseline_retention,
        }
        if baseline_source.startswith('前7日均值'):
            baseline_validation['baseline_recharge_is_arithmetic_mean'] = (
                baseline_retention is not None
            )
        validation_issues = list(dict.fromkeys(issues))
        validation_warnings = list(dict.fromkeys(warnings))
        assessment = {
            **record,
            'target_date': target_date,
            'current_values': current_values,
            'current': current_metrics,
            **baseline_validation,
            'recharge_d1': record.get('recharge_d1'),
            'validation_issues': validation_issues,
            'validation_warnings': validation_warnings,
            'quality_grade': 'P0' if validation_issues else '',
            'persistent_days': _line_persistence(
                [*detail, record], target_date, config.get('kpi', {}), record.get('system', '')
            ),
            'source_rows': line_records,
        }
        assessments.append(assessment)
    return assessments, eligible_records
