from __future__ import annotations


GRADES = ('P0', 'P1', 'P2-A', 'P2-B', 'P2-C', 'P3', 'P4')

def _effective_cpa_limit(system: str, media: str, spend, base_limits: dict) -> float | None:
    if spend is None or spend < 1000:
        return base_limits.get(system)
    media_name = (media or '').upper()
    if 'TK' in media_name:
        if system == 'iOS':
            return 45
        if system in {'安卓', 'PWA', 'H5'}:
            return 35
    return base_limits.get(system)


def _change(current, baseline):
    if current is None or baseline is None or baseline == 0:
        return None
    return current / baseline - 1


def classify(assessment: dict, config: dict) -> dict:
    rules = config.get('classification', {})
    kpi = config.get('kpi', {})
    current = assessment.get('current', {})
    baseline = assessment.get('baseline', {})
    system = assessment.get('system', '')
    media = assessment.get('media', '').strip().upper()
    spend = assessment.get('spend')
    depositors = assessment.get('level1_depositors')
    missing_thresholds = []
    triggers = []
    candidates = []
    needs_review = False

    if assessment.get('validation_issues'):
        candidates.append('P0')
        triggers.extend(assessment['validation_issues'])
        grade = 'P0'
        needs_review = True
    elif spend == 0:
        grade = 'P4'
        triggers.append('昨日无消耗')
    else:
        cpa_limit = _effective_cpa_limit(
            system,
            media,
            spend,
            kpi.get('cpa_usd', {}),
        )
        roas_floor = kpi.get('roas', {}).get(system)
        arppu_floor = kpi.get('arppu_usd', {}).get(system)
        retention_floor = kpi.get('recharge_d1', {}).get(media)

        red = {
            'CPA': cpa_limit is not None and current.get('cpa') is not None
            and current['cpa'] > cpa_limit,
            'ROAS': roas_floor is not None and current.get('roas') is not None
            and current['roas'] < roas_floor,
            'ARPPU': arppu_floor is not None and current.get('arppu') is not None
            and current['arppu'] < arppu_floor,
            '次日复充率': retention_floor is not None
            and assessment.get('recharge_d1') is not None
            and assessment['recharge_d1'] < retention_floor,
        }
        for metric, breached in red.items():
            if breached:
                trigger = (
                    'CPA高于配置上限' if metric == 'CPA'
                    else f'{metric}低于配置阈值'
                )
                triggers.append(trigger)

        missing_thresholds = [
            name for name, limit in (
                ('CPA', cpa_limit), ('ROAS', roas_floor), ('ARPPU', arppu_floor)
            ) if limit is None
        ]
        if missing_thresholds:
            triggers.append('系统KPI未配置：' + '、'.join(missing_thresholds))
            needs_review = True
        if media in {'GOOGLE', 'GOOGLE PLAY', 'APP STORE', 'APPSTORE'} \
                and retention_floor is None:
            triggers.append('该媒体次日复充率阈值未配置，未套用FB/TK标准')
            needs_review = True

        cpa_change = _change(current.get('cpa'), baseline.get('cpa'))
        roas_change = (
            current['roas'] - baseline['roas']
            if current.get('roas') is not None and baseline.get('roas') is not None
            else None
        )
        severe = []
        if (cpa_limit is not None and current.get('cpa') is not None
                and current['cpa'] >= cpa_limit * rules.get('severe_cpa_multiple', 1.2)):
            severe.append('严重CPA超标')
        if (current.get('roas') is not None
                and current['roas'] <= rules.get('severe_roas_floor', 0.2)):
            severe.append('ROAS处于严重低位')
        if (arppu_floor is not None and current.get('arppu') is not None
                and current['arppu'] <= arppu_floor * rules.get('severe_arppu_multiple', 0.7)):
            severe.append('ARPPU严重低于下限')
        triggers.extend(severe)

        high_spend = spend is not None and spend >= rules.get('high_spend_usd', 1000)
        adverse_funnel = any(
            item.get('factor') != 'cpc' and item.get('contribution', 0) > 0
            for item in assessment.get('attribution', {}).get('cpa_contributions', [])
        )
        other_risk = (
            any(red[name] for name in ('ROAS', 'ARPPU', '次日复充率'))
            or adverse_funnel
        )
        cpa_deteriorated = (
            cpa_change is not None
            and cpa_change >= rules.get('cpa_severe_increase', 0.2)
        )
        roas_deteriorated = (
            roas_change is not None
            and roas_change <= -rules.get('roas_severe_drop', 0.1)
        )
        p1 = (
            high_spend and len(severe) >= 2
            or high_spend and cpa_deteriorated and other_risk
            or high_spend and roas_deteriorated and other_risk
            or high_spend and assessment.get('persistent_days', 0) >=
            rules.get('persistent_days', 3) and len(severe) >= 1
        )
        if p1:
            grade = 'P1'
            candidates.append('P1')
            triggers.append('高消耗且满足多指标/明显恶化升级条件')
        elif (any(red.values())
              and assessment.get('persistent_days', 0) >= rules.get('persistent_days', 3)
              and not high_spend):
            grade = 'P2-A'
            candidates.append('P2-A')
            triggers.append('核心指标连续多日不达标，消耗未达高消耗升级线')
        elif any(red.values()):
            if depositors is not None and depositors < rules.get('small_sample_depositors', 10):
                grade = 'P2-C'
                candidates.append('P2-C')
                triggers.append('一级首充样本不足10人，单日结论需谨慎')
            elif not assessment.get('baseline_complete'):
                grade = 'P2-C'
                candidates.append('P2-C')
                triggers.append('前7日基线不完整或可信度不足')
                needs_review = True
            else:
                grade = 'P2-B'
                candidates.append('P2-B')
                triggers.append('存在单项或结构性指标异常，未达到P1/P2-A条件')
        elif missing_thresholds or not assessment.get('baseline_complete'):
            grade = 'P2-C'
            candidates.append('P2-C')
            if not assessment.get('baseline_complete'):
                triggers.append('基线不完整，暂不能确认趋势')
            needs_review = True
        else:
            grade = 'P3'
            triggers.append('已配置核心KPI均达标')

        if depositors is not None and depositors < rules.get('small_sample_depositors', 10):
            if grade != 'P1':
                if grade in {'P2-B', 'P3'}:
                    candidates.append(grade)
                    grade = 'P2-C'
                triggers.append('小样本提示：一级首充人数少于10')
                needs_review = True
            else:
                triggers.append('小样本但保留P1高风险级别，需优先人工复核')
                needs_review = True

    if assessment.get('validation_warnings'):
        triggers.extend(assessment['validation_warnings'])
        needs_review = True

    risk = _risk_score(assessment, config)
    confidence = 0.95
    if assessment.get('validation_issues'):
        confidence = 0.2
    elif not assessment.get('baseline_complete'):
        confidence -= 0.25
    if depositors is not None and depositors < rules.get('small_sample_depositors', 10):
        confidence -= 0.2
    if assessment.get('validation_warnings'):
        confidence -= min(0.2, len(assessment['validation_warnings']) * 0.03)
    if spend not in (None, 0) and missing_thresholds:
        confidence -= 0.1
    assessment.update({
        'auto_grade': grade,
        'effective_grade': assessment.get('manual_grade') or grade,
        'grade_triggers': list(dict.fromkeys(triggers)),
        'candidate_grades': list(dict.fromkeys(candidates)),
        'confidence': max(0.1, min(1.0, confidence)),
        'risk_score': risk,
        'needs_review': needs_review or bool(assessment.get('manual_grade')),
        'manual_reason': assessment.get('manual_reason'),
    })
    abnormal_grades = {'P0', 'P1', 'P2-A', 'P2-B', 'P2-C'}
    assessment['abnormal_days'] = (
        max(
            assessment.get('persistent_days', 0),
            (assessment.get('previous_abnormal_days', 0) + 1)
            if grade in abnormal_grades else 0,
        )
        if grade in abnormal_grades else 0
    )
    if assessment.get('manual_grade'):
        assessment['effective_grade'] = assessment['manual_grade']
    return assessment


def _risk_score(assessment: dict, config: dict) -> float:
    weights = config.get('risk_weights', {})
    current, baseline = assessment.get('current', {}), assessment.get('baseline', {})
    kpi = config.get('kpi', {})
    system = assessment.get('system', '')
    rules = config.get('classification', {})

    cpa_limit = kpi.get('cpa_usd', {}).get(system)
    roas_floor = kpi.get('roas', {}).get(system)
    arppu_floor = kpi.get('arppu_usd', {}).get(system)
    financial = 0.0
    points = []
    if cpa_limit and current.get('cpa') is not None:
        points.append(max(0.0, (current['cpa'] / cpa_limit - 1) * 100))
    if roas_floor and current.get('roas') is not None:
        points.append(max(0.0, (roas_floor - current['roas']) / roas_floor * 100))
    if arppu_floor and current.get('arppu') is not None:
        points.append(max(0.0, (arppu_floor - current['arppu']) / arppu_floor * 100))
    if points:
        financial = min(100.0, sum(points) / len(points))

    deterioration_points = []
    if current.get('cpa') and baseline.get('cpa'):
        deterioration_points.append(max(0.0, (current['cpa'] / baseline['cpa'] - 1) * 100))
    if current.get('roas') is not None and baseline.get('roas'):
        deterioration_points.append(
            max(0.0, (baseline['roas'] - current['roas']) / baseline['roas'] * 100)
        )
    deterioration = min(100.0, sum(deterioration_points) / len(deterioration_points)) \
        if deterioration_points else 0.0

    high_spend = rules.get('high_spend_usd', 1000)
    spend_score = min(100.0, (assessment.get('spend') or 0) / high_spend * 100) \
        if high_spend else 0.0
    persistence = min(
        100.0,
        assessment.get('persistent_days', 0) / max(1, rules.get('persistent_days', 3)) * 100
    )
    sample_score = 100.0 if (assessment.get('level1_depositors') or 0) < \
        rules.get('small_sample_depositors', 10) else 0.0
    if assessment.get('recharge_d1') is None:
        sample_score = max(sample_score, 25.0)

    total_weight = sum(weights.values()) or 1
    score = (
        financial * weights.get('financial', 35)
        + deterioration * weights.get('deterioration', 25)
        + spend_score * weights.get('spend', 20)
        + persistence * weights.get('persistence', 10)
        + sample_score * weights.get('retention_and_sample', 10)
    ) / total_weight
    return round(score, 1)
