from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from attribution import analyze_attribution
from classifier import GRADES, classify
from conclusion import recommendation
from history import assessment_key
from metrics import SUM_FIELDS, build_assessments


def _composite_key(record: dict) -> tuple:
    return tuple(record.get(field) for field in ('group', 'operator', 'media', 'system'))


def aggregate_to_composite(records: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for record in records:
        grouped[(
            record.get('group'), record.get('operator'), record.get('media'),
            record.get('system'), record.get('data_type'), record.get('date'),
        )].append(record)

    result = []
    for key, members in grouped.items():
        group, operator, media, system, data_type, day = key
        aggregate = {
            'group': group,
            'operator': operator,
            'media': media,
            'system': system,
            'line_id': '|'.join(str(value or '') for value in (group, operator, media, system)),
            'line_id_source': 'composite',
            'data_type': data_type,
            'date': day,
            'manual_grade': None,
            'manual_reason': None,
        }
        for field in SUM_FIELDS:
            values = [member.get(field) for member in members]
            aggregate[field] = (
                sum(values) if values and all(value is not None for value in values)
                else None
            )
        for field in ('recharge_d1',):
            values = [member.get(field) for member in members
                      if member.get(field) is not None]
            aggregate[field] = sum(values) / len(values) if values else None
        result.append(aggregate)
    return result


def run_historical_backtest(
    report_records: list[dict],
    manual_records: list[dict],
    config: dict,
    output_path: str | Path,
) -> dict:
    labels = [
        record for record in manual_records
        if record.get('data_type') == 'yesterday' and record.get('manual_grade')
    ]
    label_counts = Counter(_composite_key(record) for record in labels)
    duplicate_labels = [key for key, count in label_counts.items() if count > 1]
    if duplicate_labels:
        raise ValueError(
            f'Manual workbook has duplicate composite labels: {duplicate_labels[:5]}'
        )
    label_by_key = {_composite_key(record): record for record in labels}

    aggregated = aggregate_to_composite(report_records)
    assessments, _ = build_assessments(aggregated, config)
    auto_by_key = {
        _composite_key(item): item
        for item in assessments
        if item.get('data_type') != 'average'
    }
    rows = []
    case_assessments = []
    for key, manual in label_by_key.items():
        item = auto_by_key.get(key)
        if item is None:
            rows.append({
                '记录键': '|'.join(str(value or '') for value in key),
                '人工最终分级': manual['manual_grade'],
                '自动建议分级': '无匹配自动记录',
                '差异状态': '无法比较：无匹配自动记录',
                '人工结论': manual.get('manual_reason'),
            })
            continue
        item['manual_grade'] = manual['manual_grade']
        item['manual_reason'] = manual.get('manual_reason')
        item['attribution'] = analyze_attribution(item)
        classify(item, config)
        item['recommendation'] = recommendation(item)
        if (item.get('operator') == chr(0x7334) + chr(0x5b50)
                and item.get('group') == 'B'
                and str(item.get('media', '')).upper() == 'FB'
                and str(item.get('system', '')).casefold() == 'ios'):
            case_assessments.append(item)
        actual_metric_fields = ('spend', 'level1_depositors', 'level1_amount')
        metric_matches = all(
            item.get(field) is not None and manual.get(field) is not None
            and abs(item[field] - manual[field]) <= max(0.02, abs(manual[field]) * 0.01)
            for field in actual_metric_fields
        )
        rows.append({
            '记录键': assessment_key(item),
            '组别': item.get('group'),
            '投手': item.get('operator'),
            '媒体': item.get('media'),
            '系统': item.get('system'),
            '投放线键粒度': '组别+投手+媒体+系统（无唯一ID）',
            '自动建议分级': item['auto_grade'],
            '人工最终分级': manual['manual_grade'],
            '差异状态': '一致' if item['auto_grade'] == manual['manual_grade'] else '不一致',
            '指标汇总核对': '一致' if metric_matches else '不一致/缺失',
            '昨日消耗$': item.get('spend'),
            '人工消耗$': manual.get('spend'),
            '自动CPA$': item.get('current', {}).get('cpa'),
            '人工CPA$': manual.get('reported_cpa'),
            '自动ROAS': item.get('current', {}).get('roas'),
            '人工ROAS': manual.get('reported_roas'),
            '自动ARPPU$': item.get('current', {}).get('arppu'),
            '人工ARPPU$': manual.get('reported_arppu'),
            '触发条件': '；'.join(item.get('grade_triggers', [])),
            '人工结论': manual.get('manual_reason'),
        })

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(target, index=False, encoding='utf-8-sig')

    matched = [row for row in rows if row.get('自动建议分级') in GRADES]
    correct = sum(row['自动建议分级'] == row['人工最终分级'] for row in matched)
    by_grade = {}
    for grade in GRADES:
        true_positive = sum(
            row['人工最终分级'] == grade and row['自动建议分级'] == grade
            for row in matched
        )
        predicted = sum(row['自动建议分级'] == grade for row in matched)
        actual = sum(row['人工最终分级'] == grade for row in matched)
        by_grade[grade] = {
            'precision': true_positive / predicted if predicted else None,
            'recall': true_positive / actual if actual else None,
            'true_positive': true_positive,
            'predicted_count': predicted,
            'manual_count': actual,
        }
    confusion = {
        manual_grade: {
            auto_grade: sum(
                row['人工最终分级'] == manual_grade
                and row['自动建议分级'] == auto_grade
                for row in matched
            )
            for auto_grade in GRADES
        }
        for manual_grade in GRADES
    }
    misses = {
        grade: [
            {
                'record_key': row.get('记录键'),
                'operator': row.get('投手'),
                'media': row.get('媒体'),
                'system': row.get('系统'),
                'manual_grade': row.get('人工最终分级'),
                'auto_grade': row.get('自动建议分级'),
                'trigger': row.get('触发条件'),
            }
            for row in matched
            if row['人工最终分级'] == grade and row['自动建议分级'] != grade
        ]
        for grade in ('P0', 'P1')
    }
    expected_case = {
        'spend': 2812.53,
        'cpa': 40.18,
        'baseline_cpa': 33.42,
        'roas': 0.304,
        'baseline_roas': 0.307,
        'cpc': 0.86,
        'baseline_cpc': 0.96,
        'click_install_rate': 0.046,
        'baseline_click_install_rate': 0.060,
        'install_registration_rate': 0.733,
        'baseline_install_registration_rate': 0.807,
        'registration_deposit_rate': 0.636,
        'baseline_registration_deposit_rate': 0.593,
    }
    case = None
    if case_assessments:
        item = case_assessments[0]
        current, baseline = item.get('current', {}), item.get('baseline', {})
        actual_case = {
            'spend': item.get('spend'),
            'cpa': current.get('cpa'),
            'baseline_cpa': baseline.get('cpa'),
            'roas': current.get('roas'),
            'baseline_roas': baseline.get('roas'),
            'cpc': current.get('cpc'),
            'baseline_cpc': baseline.get('cpc'),
            'click_install_rate': current.get('click_install_rate'),
            'baseline_click_install_rate': baseline.get('click_install_rate'),
            'install_registration_rate': current.get('install_registration_rate'),
            'baseline_install_registration_rate': baseline.get('install_registration_rate'),
            'registration_deposit_rate': current.get('registration_deposit_rate'),
            'baseline_registration_deposit_rate': baseline.get('registration_deposit_rate'),
        }
        tolerances = {
            'spend': 0.01, 'cpa': 0.02, 'baseline_cpa': 0.02,
            'roas': 0.001, 'baseline_roas': 0.001,
            'cpc': 0.01, 'baseline_cpc': 0.01,
            'click_install_rate': 0.001, 'baseline_click_install_rate': 0.001,
            'install_registration_rate': 0.001,
            'baseline_install_registration_rate': 0.001,
            'registration_deposit_rate': 0.001,
            'baseline_registration_deposit_rate': 0.001,
        }
        checks = {
            field: {
                'expected': expected_case[field],
                'observed': actual_case[field],
                'matches': actual_case[field] is not None and
                abs(actual_case[field] - expected_case[field]) <= tolerances[field],
            }
            for field in expected_case
        }
        case = {
            'found': True,
            'aggregate_spend_matches': checks['spend']['matches'],
            'checks': checks,
            'overall_match': all(value['matches'] for value in checks.values()),
            'automatic_grade': item.get('auto_grade'),
            'manual_grade': item.get('manual_grade'),
            'cpa_change_percent': (
                item['attribution'].get('cpa_change') * 100
                if item['attribution'].get('cpa_change') is not None else None
            ),
            'attribution_reliable': item['attribution'].get('reliable'),
            'cpa_contributions': item['attribution'].get('cpa_contributions', []),
            'cpa_summary': item['attribution'].get('cpa_summary'),
        }
    else:
        case = {
            'found': False,
            'status': 'No matching Monkey/B/FB/iOS aggregate in supplied benchmark.',
        }
    suggestions = []
    if misses['P1']:
        suggestions.append(
            f"{len(misses['P1'])}条人工P1未被自动识别；复核高消耗及相对恶化升级门槛，"
            '尤其检查漏斗恶化证据是否足以触发P1。'
        )
    if misses['P0']:
        suggestions.append(
            f"{len(misses['P0'])}条人工P0未被自动识别；检查人工P0原因是否属于"
            '当前必填、分母为0、重复或非法负数校验范围。'
        )
    if by_grade['P2-A']['manual_count'] == 0:
        suggestions.append(
            '本批人工标签没有P2-A样例，暂不能验证持续低效级别的精确率/召回率。'
        )
    metric_mismatches = [
        row for row in matched if row.get('指标汇总核对') != '一致'
    ]
    if metric_mismatches:
        suggestions.append(
            f'{len(metric_mismatches)}条手工汇总与详细报表分子/分母未完全对齐，'
            '建议先核对来源口径后再调整分类阈值。'
        )
    return {
        'manual_label_count': len(labels),
        'matched_count': len(matched),
        'unmatched_count': len(labels) - len(matched),
        'overall_agreement': correct / len(matched) if matched else None,
        'confusion_matrix_manual_rows_auto_columns': confusion,
        'per_grade': by_grade,
        'p0_misses': misses['P0'],
        'p1_misses': misses['P1'],
        'mismatch_count': len(matched) - correct,
        'duplicate_manual_keys': len(duplicate_labels),
        'matched_metric_check_count': sum(
            row.get('指标汇总核对') == '一致' for row in matched
        ),
        'rule_adjustment_suggestions': suggestions,
        'known_case_check': case,
        'aggregation_grain': '组别+投手+媒体+系统; no line ID in manual workbook',
        'difference_csv': str(target),
    }
