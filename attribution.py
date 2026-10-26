from __future__ import annotations

import math


FUNNEL_LABELS = {
    'cpc': 'CPC上涨（流量点击成本）',
    'click_install_rate': '点击安装率下降',
    'install_registration_rate': '安装注册率下降',
    'registration_deposit_rate': '注册首充率下降',
}


def analyze_attribution(assessment: dict) -> dict:
    current = assessment.get('current', {})
    baseline = assessment.get('baseline', {})
    result = {
        'reliable': True,
        'cpa_contributions': [],
        'roas_contributions': [],
        'roas_summary': '基线或昨日财务指标不足，无法可靠分解ROAS变化。',
    }
    required = ('cpc', 'click_install_rate', 'install_registration_rate',
                'registration_deposit_rate')
    pairs = [(current.get(field), baseline.get(field)) for field in required]
    if any(today is None or previous is None or today <= 0 or previous <= 0
           for today, previous in pairs):
        result['reliable'] = False
        result['cpa_summary'] = '漏斗分子/分母缺失或为0，CPA归因不可靠。'
    elif not assessment.get('baseline_complete'):
        result['reliable'] = False
        result['cpa_summary'] = '前7日基线不完整，CPA归因仅供参考。'
    else:
        contributions = []
        for field, (today, previous) in zip(required, pairs):
            factor = math.log(today / previous)
            if field != 'cpc':
                factor = -factor
            contributions.append({
                'factor': field,
                'label': FUNNEL_LABELS[field],
                'contribution': factor,
            })
        for side, metric_values in (
            ('昨日', current), ('前7日', baseline)
        ):
            funnel_values = [metric_values.get(field) for field in required]
            cpa = metric_values.get('cpa')
            if (cpa is None or cpa <= 0
                    or any(value is None or value <= 0 for value in funnel_values)):
                result['reliable'] = False
                break
            funnel_cpa = funnel_values[0] / (
                funnel_values[1] * funnel_values[2] * funnel_values[3]
            )
            if abs(math.log(funnel_cpa / cpa)) > math.log(1.05):
                result['reliable'] = False
                result['cpa_summary'] = (
                    f'{side}漏斗指标与CPA不一致，CPA归因不可靠。'
                )
                break
        if result['reliable']:
            result['cpa_contributions'] = sorted(
                contributions, key=lambda item: item['contribution'], reverse=True
            )
            top = [item for item in result['cpa_contributions']
                   if item['contribution'] > 0][:2]
            result['cpa_summary'] = (
                '主要指标贡献来自' + '、'.join(item['label'] for item in top)
                if top else '未发现推高CPA的正向漏斗贡献项。'
            )
        else:
            result['cpa_contributions'] = []
            if not result.get('cpa_summary'):
                result['cpa_summary'] = '漏斗指标与CPA未能对齐，CPA归因不可靠。'

    current_cpa, baseline_cpa = current.get('cpa'), baseline.get('cpa')
    current_arppu, baseline_arppu = current.get('arppu'), baseline.get('arppu')
    current_roas, baseline_roas = current.get('roas'), baseline.get('roas')
    if all(value is not None and value > 0 for value in
           (current_cpa, baseline_cpa, current_arppu, baseline_arppu)):
        cpa_change = math.log(current_cpa / baseline_cpa)
        arppu_change = math.log(current_arppu / baseline_arppu)
        result['roas_contributions'] = [
            {'factor': 'CPA', 'log_contribution': -cpa_change},
            {'factor': 'ARPPU', 'log_contribution': arppu_change},
        ]
        if cpa_change > 0 and arppu_change < 0:
            summary = '获客成本上升与首充客单价下降共同承压'
        elif cpa_change > 0:
            summary = '主要表现为获客成本压力'
        elif arppu_change < 0:
            summary = '主要表现为首充客单价下降'
        elif cpa_change < 0 and arppu_change > 0:
            summary = 'CPA下降且ARPPU上升，效率改善'
        else:
            summary = 'CPA与ARPPU变化方向相近，需结合ROAS核对'
        if current_roas is not None and baseline_roas is not None:
            summary += f'；ROAS变化{(current_roas - baseline_roas) * 100:+.1f}个百分点'
        result['roas_summary'] = summary
    result['cpa_change'] = (
        current_cpa / baseline_cpa - 1
        if current_cpa is not None and baseline_cpa else None
    )
    result['roas_change'] = (
        current_roas - baseline_roas
        if current_roas is not None and baseline_roas is not None else None
    )
    result['arppu_change'] = (
        current_arppu / baseline_arppu - 1
        if current_arppu is not None and baseline_arppu else None
    )
    return result
