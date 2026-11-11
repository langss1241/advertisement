from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from attribution import analyze_attribution
from conclusion import make_conclusion, recommendation
from history import assessment_key


GRADE_ORDER = {'P0': 0, 'P1': 1, 'P2-A': 2, 'P2-B': 3, 'P2-C': 4, 'P3': 5, 'P4': 6}


def _coalesce(item: dict, *keys, default=None):
    for key in keys:
        value = item.get(key)
        if value is not None and value != '':
            return value
    return default


def _display(item: dict) -> dict:
    current = item.get('current', {})
    baseline = item.get('baseline', {})
    attribution = item.get('attribution', {})
    cpa_change = attribution.get('cpa_change')
    return {
        '日期': item['target_date'].isoformat(),
        '记录键': assessment_key(item),
        '投放线ID': item.get('line_id'),
        '组别': item.get('group'),
        '投手': item.get('operator'),
        '渠道/投放线': item.get('line_id'),
        '媒体': item.get('media'),
        '系统': item.get('system'),
        '实际消耗$': item.get('spend'),
        '成本偏离阈值%': _coalesce(item, 'cost_deviation', '成本偏离阈值%', '成本偏离阀值%', '超成本阈值', '超成本阀值', default=None),
        '一级首充人数': item.get('level1_depositors'),
        'CPA$': current.get('cpa'),
        'ROAS': current.get('roas'),
        'ARPPU$': current.get('arppu'),
        '次日复充率': item.get('recharge_d1'),
        '前7日CPA$': baseline.get('cpa'),
        '前7日ROAS': baseline.get('roas'),
        '前7日ARPPU$': baseline.get('arppu'),
        'CPA变化': cpa_change,
        'ROAS变化百分点': attribution.get('roas_change'),
        '分级': _coalesce(item, 'effective_grade', 'actual_grade', 'auto_grade', 'manual_grade', default=None),
        # '昨日生效分级': item.get('previous_grade'),
        # '人工最终分级': item.get('manual_grade'),
        # '实际生效分级': item.get('effective_grade'),
        '异常持续天数': item.get('abnormal_days'),
        '新增异常': '是' if item.get('new_anomaly') else '否',
        '风险变化': item.get('grade_change'),
        '风险分数': item.get('risk_score'),
        '分级置信度': item.get('confidence'),
        '分级触发条件': '；'.join(item.get('grade_triggers', [])),
        '数据校验问题': '；'.join(item.get('validation_issues', [])),
        '数据质量提醒': '；'.join(item.get('validation_warnings', [])),
        'CPA主要贡献': attribution.get('cpa_summary'),
        'ROAS初步归因': attribution.get('roas_summary'),
        # '前7日基线来源': item.get('baseline_source'),
        '前7日有效天数': item.get('baseline_days'),
        # '前7日基线完整': item.get('baseline_complete'),
        '完整结论': _coalesce(item, 'conclusion', '完整结论', default=None),
        '人工最终结论': _coalesce(item, 'manual_conclusion', '人工最终结论', default=None),
        '处理建议': item.get('recommendation'),
        '需要人工复核': '是' if item.get('needs_review') else '否',
        '人工修正原因': _coalesce(item, 'manual_reason', '人工修正原因', default=None),
        '实际处理动作': item.get('actual_action'),
        '后续效果': item.get('follow_up'),
    }


def _sort(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (
        GRADE_ORDER.get(item.get('effective_grade'), 99),
        -item.get('risk_score', 0),
        -float(item.get('spend') or 0),
    ))


def _trend_frame(
    records: list[dict], group: str, assessments_by_line_date: dict | None = None
) -> pd.DataFrame:
    group_records = [item for item in records if item.get('group') == group]
    rows = []
    for item in group_records:
        metrics = item.get('derived', {})
        assessment_key = (
            item.get('date'), item.get('group'), item.get('line_id'),
            item.get('operator'), item.get('media'), item.get('system'),
        )
        assessment = (assessments_by_line_date or {}).get(assessment_key, {})
        cost_deviation = _coalesce(
            item, 'cost_deviation', '成本偏离阈值%', '成本偏离阀值%', '超成本阈值', '超成本阀值', default=None
        )
        manual_grade = _coalesce(item, 'manual_grade', '人工最终分级', '分级', default=None)
        effective_grade = _coalesce(item, 'effective_grade', '实际生效分级', default=None)
        manual_reason = _coalesce(item, 'manual_reason', '人工修正原因', '结论', default=None)
        manual_conclusion = _coalesce(item, 'manual_conclusion', '人工最终结论', default=None)
        conclusion = _coalesce(item, 'conclusion', '完整结论', default=None)
        rows.append({
            '数据类型': item.get('data_type'),
            '日期': item.get('date'),
            '投放线ID': item.get('line_id'),
            '组别': item.get('group'),
            '投手': item.get('operator'),
            '媒体': item.get('media'),
            '系统': item.get('system'),
            '实际消耗$': item.get('spend'),
            '一级首充人数': item.get('level1_depositors'),
            '成本偏离阈值%': cost_deviation,
            'CPA$': metrics.get('cpa'),
            'ROAS': metrics.get('roas'),
            'ARPPU$': metrics.get('arppu'),
            '分级': manual_grade or effective_grade or assessment.get('effective_grade'),
            '结论': (
                manual_reason
                or manual_conclusion
                or conclusion
                or assessment.get('manual_conclusion')
                or assessment.get('conclusion')
            ),
            '首充人数': item.get('first_depositors'),
            '曝光量': item.get('impressions'),
            '点击量': item.get('clicks'),
            'CPC$': metrics.get('cpc'),
            '安装量': item.get('installs'),
            '安装成本$': metrics.get('install_cpa'),
            '一级注册人数': item.get('registrations'),
            '点击安装率': metrics.get('click_install_rate'),
            '安装注册率': metrics.get('install_registration_rate'),
            '一级注册首充率': metrics.get('registration_deposit_rate'),
            '次日复充率': item.get('recharge_d1'),
        })
    return pd.DataFrame(rows)


def _format_sheet(sheet, header_row: int = 1):
    sheet.freeze_panes = f'A{header_row + 1}'
    sheet.auto_filter.ref = (
        f'A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}'
    )
    sheet.row_dimensions[header_row].height = 30
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F4E78')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        max_len = min(max(
            len(str(cell.value or '')) if cell.data_type != 'f' else 12
            for cell in list(column)[:50]
        ) + 2, 46)
        sheet.column_dimensions[letter].width = max(12, max_len)
    headers = {
        cell.value: cell.column for cell in sheet[header_row]
        if cell.value is not None
    }
    accent_fill = 'C65911'
    data_fill = 'FCE4D6'
    accent_labels = {'一级首充人数', '成本偏离阈值%', 'CPA$', 'ROAS', 'ARPPU$'}
    for label, color in (
        ('分级', '806000'),
        ('人工最终分级', '1F4E78'),
    ):
        if label in headers:
            sheet.cell(header_row, headers[label]).fill = PatternFill('solid', fgColor=color)
    for label in accent_labels:
        if label in headers:
            column_index = headers[label]
            sheet.cell(header_row, column_index).fill = PatternFill('solid', fgColor=accent_fill)
            for row in range(header_row + 1, sheet.max_row + 1):
                sheet.cell(row, column_index).fill = PatternFill('solid', fgColor=data_fill)
    for label in ('ROAS', '次日复充率', '前7日ROAS', 'CPA变化', 'ROAS变化百分点',
                  '分级置信度', '点击安装率', '安装注册率', '一级注册首充率',
                  '成本偏离阈值%'):
        if label in headers:
            for row in range(header_row + 1, sheet.max_row + 1):
                sheet.cell(row, headers[label]).number_format = '0.0%'
    if '风险分数' in headers:
        for row in range(header_row + 1, sheet.max_row + 1):
            sheet.cell(row, headers['风险分数']).number_format = '0.0'
    for label, column_index in headers.items():
        if label.endswith('$'):
            for row in range(header_row + 1, sheet.max_row + 1):
                sheet.cell(row, column_index).number_format = '$#,##0.00;[Red]-$#,##0.00'
    for label in ('完整结论', '人工最终结论', '分级触发条件', '数据校验问题',
                  '数据质量提醒', '处理建议', '人工修正原因'):
        if label in headers:
            for row in range(header_row + 1, sheet.max_row + 1):
                sheet.cell(row, headers[label]).alignment = Alignment(
                    wrap_text=True, vertical='top'
                )
    if '实际生效分级' in headers and sheet.max_row >= 2:
        column = get_column_letter(headers['实际生效分级'])
        colors = {
            'P0': 'F4CCCC', 'P1': 'FCE4D6', 'P2-A': 'FFF2CC',
            'P2-B': 'FCE4D6', 'P2-C': 'DDEBF7', 'P3': 'E2F0D9', 'P4': 'E7E6E6',
        }
        for grade, color in colors.items():
            sheet.conditional_formatting.add(
                f'{column}{header_row + 1}:{column}{sheet.max_row}',
                FormulaRule(
                    formula=[f'${column}{header_row + 1}="{grade}"'],
                    fill=PatternFill('solid', fgColor=color),
                ),
            )


def write_workbook(
    output: str | Path,
    assessments: list[dict],
    records: list[dict],
    config: dict,
) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = _sort(assessments)
    assessments_by_line_date = {
        (
            item.get('target_date'), item.get('group'), item.get('line_id'),
            item.get('operator'), item.get('media'), item.get('system'),
        ): item
        for item in ordered
    }
    table = pd.DataFrame([_display(item) for item in ordered])
    preferred_order = [
        '分级', '日期', '记录键', '组别', '投手', '媒体', '系统',
        '实际消耗$', '一级首充人数', '成本偏离阈值%', 'CPA$', 'ARPPU$', 'ROAS','前7日CPA$', '前7日ROAS', '前7日ARPPU$', 'CPA变化', 'ROAS变化百分点',
         'CPA主要贡献', 'ROAS初步归因',
        '风险分数', '分级置信度', '完整结论', '处理建议',
        '人工最终结论', '人工修正原因', '实际处理动作', '后续效果'
    ]
    for column in [ '分级','异常持续天数',
                   '新增异常', '风险变化', '分级触发条件', '数据校验问题', 
                   'CPA主要贡献', 'ROAS初步归因', '前7日有效天数', 
                   '需要人工复核', '前7日CPA$', '前7日ROAS', '前7日ARPPU$', 'CPA变化', 'ROAS变化百分点',
                   '次日复充率', '实际消耗$']:
        if column in table.columns and column not in preferred_order:
            preferred_order.append(column)
    table = table[[column for column in preferred_order if column in table.columns]]
    ranked = [item for item in ordered if item.get('effective_grade') not in {'P0', 'P4'}]
    top20 = pd.DataFrame([_display(item) for item in ranked[:20]])
    deep = sorted(
        ranked,
        key=lambda item: (
            0 if item.get('effective_grade') == 'P1' else 1,
            -item.get('risk_score', 0),
        ),
    )[:10]
    deep_frame = pd.DataFrame([_display(item) for item in deep])

    review_columns = [
        '日期', '记录键', '投放线ID', '组别', '投手', '媒体', '系统',
        '分级', '昨日生效分级', '人工最终分级', '实际生效分级',
        '异常持续天数', '新增异常', '风险变化', '分级触发条件',
        '分级置信度', '风险分数', '完整结论', '处理建议',
        '人工最终结论', '人工修正原因', '实际处理动作', '后续效果',
    ]
    review = pd.DataFrame([
        {**{column: row.get(column) for column in review_columns},
         '人工修正原因': item.get('manual_reason'),
         '实际处理动作': item.get('actual_action'),
         '后续效果': item.get('follow_up')}
        for item, row in zip(ordered, [_display(value) for value in ordered])
    ], columns=review_columns)
    review['记录键'] = [assessment_key(item) for item in ordered]

    rules = []
    for group, values in config.get('kpi', {}).items():
        for name, value in values.items():
            rules.append({'类别': group, '媒体/系统': name, '配置值': value})
    for key, value in config.get('classification', {}).items():
        rules.append({'类别': '初始规则（需人工校准）', '媒体/系统': key, '配置值': value})
    rules.append({
        '类别': '数据来源说明',
        '媒体/系统': '复充率为每日比例的算术平均，非队列加权值；缺失不视为0',
        '配置值': '适用数据条件下',
    })
    rules_frame = pd.DataFrame(rules)

    p0 = pd.DataFrame([_display(item) for item in ordered
                       if item.get('effective_grade') == 'P0'])
    forced = [item for item in ordered if item.get('effective_grade') in {'P0', 'P1'}]
    capacity_rows = [{
        '提示': '强制处理事项超过TOP10容量；超出事项未隐藏，请查看本表。',
        'P0/P1数量': len(forced),
        'TOP10外事项数': max(0, len(forced) - 10),
    }] if len(forced) > 10 else [{
        '提示': '强制处理事项未超过TOP10容量。',
        'P0/P1数量': len(forced),
        'TOP10外事项数': 0,
    }]
    overflow = pd.DataFrame([
        _display(item) for item in _sort(forced)[10:]
    ]) if len(forced) > 10 else pd.DataFrame(columns=list(_display(ordered[0])) if ordered else [])

    grades = ('P0', 'P1', 'P2-A', 'P2-B', 'P2-C', 'P3')

    grades_labels = {
    'P0': 'P0-数据缺失',
    'P1': 'P1-重点关注',
    'P2-A': 'P2-A-持续低效',
    'P2-B': 'P2-B-单项、结构异常',
    'P2-C': 'P2-C-小样本',
    'P3': 'P3-正常达标'
    }

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        table.to_excel(writer, sheet_name='速览', index=False, startrow=4)
        overview = writer.book['速览']

        grade_columns = [
            (f'{grades_labels.get(grade, grade)}',
                sum(item.get('effective_grade') == grade for item in ordered))
            for grade in grades
            ]
        summary_columns = [
            ('投手数', len({item.get('operator') for item in ordered if item.get('operator')})),
            ('渠道数', len({item.get('line_id') for item in ordered if item.get('line_id')})),
            ('消耗', sum(float(item.get('spend') or 0) for item in ordered)),
            ('CPA$', sum(float(item.get('spend') or 0) for item in ordered)
                / sum(float(item.get('level1_depositors') or 0) for item in ordered)
                if sum(float(item.get('level1_depositors') or 0) for item in ordered) else None
            ),
            ('一级首充人数', sum(float(item.get('level1_depositors') or 0) for item in ordered)),
            ('一级ARPPU值$', (
                sum(float(item.get('level1_amount') or 0) for item in ordered)
                / sum(float(item.get('level1_depositors') or 0) for item in ordered)
                if sum(float(item.get('level1_depositors') or 0) for item in ordered) else None
            )),
            ('一级ROAS', (
                sum(float(item.get('level1_amount') or 0) for item in ordered)
                / sum(float(item.get('spend') or 0) for item in ordered)
                if sum(float(item.get('spend') or 0) for item in ordered) else None
            )),
        ]
        combined_columns = summary_columns + grade_columns
        accent_fill = 'C65911'
        data_fill = 'FCE4D6'
        highlight_names = {'消耗', 'CPA$', '一级首充人数', '一级ARPPU值$', '一级ROAS'}
        for column, (name, value) in enumerate(combined_columns, start=1):
            overview.cell(2, column, name)
            overview.cell(3, column, value)
            if name in highlight_names:
                overview.cell(2, column).font = Font(bold=True, color='FFFFFF')
                overview.cell(2, column).fill = PatternFill('solid', fgColor=accent_fill)
                overview.cell(2, column).alignment = Alignment(horizontal='center')
                overview.cell(3, column).font = Font(bold=True)
                overview.cell(3, column).fill = PatternFill('solid', fgColor=data_fill)
            else:
                overview.cell(2, column).font = Font(bold=True, color='FFFFFF')
                overview.cell(2, column).fill = PatternFill('solid', fgColor='1F4E78')
                overview.cell(2, column).alignment = Alignment(horizontal='center')
                overview.cell(3, column).font = Font(bold=True)
            if name in {'消耗', 'CPA$', '一级ARPPU值$'}:
                overview.cell(3, column).number_format = '$#,##0.00;[Red]-$#,##0.00'
            elif name == '一级ROAS':
                overview.cell(3, column).number_format = '0.0%'
        deep_frame.to_excel(writer, sheet_name='重点监控top10', index=False)
        for group in ('A', 'B', 'C', 'D'):
            _trend_frame(records, group, assessments_by_line_date).to_excel(
                writer, sheet_name=f'{group}组', index=False
            )
        rules_frame.to_excel(writer, sheet_name='分级规则', index=False)
        # review.to_excel(writer, sheet_name='人工复核记录', index=False)
        # p0.to_excel(writer, sheet_name='P0数据核验', index=False)
        # pd.DataFrame(capacity_rows).to_excel(writer, sheet_name='处理容量提示', index=False)
        # overflow.to_excel(writer, sheet_name='超容量告警', index=False)
        for sheet in writer.book.worksheets:
            _format_sheet(sheet, header_row=5 if sheet.title == '速览' else 1)


def write_backtest_diff(path: str | Path, assessments: list[dict]) -> dict:
    rows = []
    labelled = 0
    matched = 0
    for item in assessments:
        manual = item.get('manual_grade')
        if manual:
            labelled += 1
            if manual == item['auto_grade']:
                matched += 1
        rows.append({
            '日期': item['target_date'].isoformat(),
            '投放线ID': item.get('line_id'),
            '组别': item.get('group'),
            '投手': item.get('operator'),
            '媒体': item.get('media'),
            '系统': item.get('system'),
            '分级': item.get('auto_grade'),
            '人工最终分级': manual or '未提供人工标签',
            '差异状态': (
                '一致' if manual == item['auto_grade'] else
                '不一致' if manual else '无法比较：未提供人工标签'
            ),
            '触发条件': '；'.join(item.get('grade_triggers', [])),
        })
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(target, index=False, encoding='utf-8-sig')
    return {
        'total': len(rows),
        'labelled': labelled,
        'agreement': matched / labelled if labelled else None,
        'unlabelled': len(rows) - labelled,
    }
