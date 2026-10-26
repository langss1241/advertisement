from __future__ import annotations

from attribution import analyze_attribution


def _money(value, decimals=2):
    return '缺失' if value is None else f'${value:,.{decimals}f}'


def _percent(value, decimals=1):
    return '缺失' if value is None else f'{value * 100:.{decimals}f}%'


def _change(value, suffix='%'):
    return '无法计算' if value is None else f'{value * 100:+.1f}{suffix}'


def make_conclusion(assessment: dict) -> str:
    current = assessment.get('current', {})
    baseline = assessment.get('baseline', {})
    attribution = analyze_attribution(assessment)
    line = (
        f"【{assessment.get('operator') or '未知投手'}】｜"
        f"【{assessment.get('group') or '未知组'}】｜"
        f"【{assessment.get('media') or '未知媒体'} {assessment.get('system') or '未知系统'}】｜"
        f"【{assessment.get('effective_grade') or assessment.get('auto_grade')}】"
    )
    intro = (
        f"昨日消耗{_money(assessment.get('spend'))}，CPA {_money(current.get('cpa'))}、"
        f"ROAS {_percent(current.get('roas'))}、ARPPU {_money(current.get('arppu'))}、"
        f"次日复充率{_percent(assessment.get('recharge_d1'))}。"
    )
    cpa_change = attribution.get('cpa_change')
    roas_change = attribution.get('roas_change')
    arppu_change = attribution.get('arppu_change')
    change_text = (
        f"较前7日，CPA变化{_change(cpa_change)}，"
        f"ROAS变化{_change(roas_change, '个百分点')}，ARPPU变化{_change(arppu_change)}。"
    )
    changes = []
    if cpa_change is not None and cpa_change > 0:
        changes.append(f'CPA上升{_change(cpa_change)}')
    if roas_change is not None and roas_change < 0:
        changes.append(f'ROAS下降{abs(roas_change) * 100:.1f}个百分点')
    if arppu_change is not None and arppu_change < 0:
        changes.append(f'ARPPU下降{abs(arppu_change) * 100:.1f}%')
    if attribution.get('reliable'):
        changes.append(attribution.get('cpa_summary', ''))
    elif attribution.get('cpa_summary'):
        changes.append(attribution['cpa_summary'])
    if not changes and assessment.get('validation_issues'):
        changes = assessment['validation_issues'][:2]
    if not changes and assessment.get('validation_warnings'):
        changes = assessment['validation_warnings'][:2]
    observed = '主要异常：' + '；'.join(changes[:2]) + '。' if changes else '主要异常：未见明显异常。'
    recommendations = {
        'P0': '建议先核验数据，暂不据此调整投放。',
        'P1': '建议暂停加量并优先人工复核成本与回收风险；不自动调整预算。',
        'P2-A': '建议限制扩量并设定优化期限，跟踪是否恢复。',
        'P2-B': '建议保留有效流量，定向优化未达标环节。',
        'P2-C': '建议结合连续3日及7日趋势观察，避免因小样本单日波动大幅调整。',
        'P3': '建议常规监控。',
        'P4': '无消耗，单独观察，不纳入活跃线风险排序。',
    }
    recommendation = recommendations.get(
        assessment.get('effective_grade') or assessment.get('auto_grade'), '建议人工复核。'
    )
    quality = assessment.get('validation_warnings', [])
    if quality and '次日复充率缺失' in quality[0]:
        observed += ' 留存缺失，不据此推断留存差。'
    return f'{line}\n{intro}\n{change_text}\n{observed}\n建议：{recommendation}'


def recommendation(assessment: dict) -> str:
    grade = assessment.get('effective_grade') or assessment.get('auto_grade')
    text = {
        'P0': '核验原始数据及归因口径；暂不依据异常指标决策。',
        'P1': '暂停加量，检查异常漏斗及高消耗线；控量建议由人工决策。',
        'P2-A': '限制扩量，设置优化期限并跟踪连续表现。',
        'P2-B': '保留有效流量，针对未达标维度定向优化。',
        'P2-C': '观察连续3日和7日趋势，谨慎处理小样本波动。',
        'P3': '常规监控。',
        'P4': '无消耗线单独跟踪。',
    }
    return text.get(grade, '人工复核。')
