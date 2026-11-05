import math
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from attribution import analyze_attribution
from backtest import run_historical_backtest
from classifier import classify
from data_loader import normalize_frame
from history import GradeHistory, assessment_key
from metrics import build_assessments, derive_metrics
from report import write_workbook
from validation import validate_record


CONFIG = {
    'eligible_groups': ['A', 'B', 'C'],
    'kpi': {
        'cpa_usd': {'安卓': 25, 'iOS': 35},
        'roas': {'安卓': 0.3, 'iOS': 0.3},
        'arppu_usd': {'安卓': 8, 'iOS': 10},
        'recharge_d1': {'FB': 0.2, 'TK': 0.23},
    },
    'classification': {
        'high_spend_usd': 1000,
        'cpa_severe_increase': 0.2,
        'roas_severe_drop': 0.1,
        'severe_cpa_multiple': 1.2,
        'severe_roas_floor': 0.2,
        'severe_arppu_multiple': 0.7,
        'persistent_days': 3,
        'small_sample_depositors': 10,
    },
    'risk_weights': {
        'financial': 35, 'deterioration': 25, 'spend': 20,
        'persistence': 10, 'retention_and_sample': 10,
    },
}


def make_report(
    target=date(2026, 10, 22), current=None, baseline_days=7,
    group='B', operator='Test', line='FB-Test-iOS', media='FB', system='iOS',
):
    current = current or {
        'spend': 1000, 'level1_depositors': 40, 'first_depositors': 40,
        'level1_amount': 500, 'impressions': 10000, 'clicks': 1000,
        'installs': 100, 'registrations': 80, 'recharge_d1': 0.25,
    }
    rows = [{
        '数据类型': '昨日', '日期': target.isoformat(), '投手': operator,
        '组别': group, '媒体': media, '系统': system, '渠道': line,
        '实际消耗$': current.get('spend'),
        '一级首充人数': current.get('level1_depositors'),
        '首充人数': current.get('first_depositors'),
        '一级首充金额$': current.get('level1_amount'),
        '曝光量': current.get('impressions'), '点击量': current.get('clicks'),
        '安装量': current.get('installs'), '一级注册人数': current.get('registrations'),
        '次日复充率': current.get('recharge_d1'),
    }]
    for offset in range(1, baseline_days + 1):
        rows.append({
            '数据类型': '前7日明细', '日期': (target - timedelta(days=offset)).isoformat(),
            '投手': operator, '组别': group, '媒体': media, '系统': system,
            '渠道': line, '实际消耗$': 1000, '一级首充人数': 40,
            '首充人数': 40, '一级首充金额$': 500,
            '曝光量': 10000, '点击量': 1000, '安装量': 100,
            '一级注册人数': 80, '次日复充率': 0.25,
        })
    return normalize_frame(pd.DataFrame(rows))[0]


def assessment_for(report, config=None):
    assessments, _ = build_assessments(report, config or CONFIG)
    return assessments[0]


def grade(report, config=None):
    assessment = assessment_for(report, config)
    assessment['attribution'] = analyze_attribution(assessment)
    return classify(assessment, config or CONFIG)


class SystemTests(unittest.TestCase):
    def test_backtest_aggregates_to_manual_composite_key(self):
        report = make_report(line='channel-1') + make_report(line='channel-2')
        manual_frame = pd.DataFrame([{
            '数据类型': '昨日', '日期': '2026-10-22', '投手': 'Test', '组别': 'B',
            '媒体': 'FB', '系统': 'iOS', '实际消耗$': 2000,
            '一级首充人数': 80, '首充人数': 80, '一级首充金额$': 1000,
            '分级': 'P3',
        }])
        manual, _ = normalize_frame(manual_frame)
        with tempfile.TemporaryDirectory() as directory:
            result = run_historical_backtest(
                report, manual, CONFIG, Path(directory) / 'backtest.csv'
            )
        self.assertEqual(result['manual_label_count'], 1)
        self.assertEqual(result['matched_count'], 1)
        self.assertAlmostEqual(result['overall_agreement'], 1.0)
        self.assertEqual(result['matched_metric_check_count'], 1)

    def test_weighted_seven_day_baseline(self):
        rows = make_report()
        for row in rows:
            if row.get('data_type') == 'daily':
                row['spend'] = 100 if row['date'].day % 2 else 900
                row['level1_depositors'] = 10 if row['date'].day % 2 else 90
                row['level1_amount'] = 100 if row['date'].day % 2 else 900
        item = assessment_for(rows)
        expected_spend = sum(r['spend'] for r in rows if r['data_type'] == 'daily')
        expected_depositors = sum(
            r['level1_depositors'] for r in rows if r['data_type'] == 'daily'
        )
        self.assertAlmostEqual(item['baseline']['cpa'], expected_spend / expected_depositors)

    def test_incomplete_baseline_is_marked_not_p0(self):
        item = grade(make_report(baseline_days=5))
        self.assertFalse(item['validation_issues'])
        self.assertFalse(item['baseline_complete'])
        self.assertIn('基线不完整', '；'.join(item['grade_triggers']))

    def test_duplicate_line_date_is_p0(self):
        rows = make_report()
        rows.append(dict(rows[0]))
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P0')

    def test_negative_metrics_are_p0(self):
        rows = make_report(current={
            'spend': -5, 'level1_depositors': 1, 'level1_amount': 20,
            'impressions': 10, 'clicks': 2, 'installs': 1,
            'registrations': 1, 'recharge_d1': None,
        })
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P0')

    def test_zero_denominator_with_valid_conversion_is_p0(self):
        record = {
            'operator': 'x', 'group': 'A', 'media': 'FB', 'system': 'iOS',
            'data_type': 'yesterday', 'date': date(2026, 10, 22),
            'spend': 5, 'level1_depositors': 1, 'level1_amount': 1,
            'clicks': 0, 'installs': 1, 'registrations': 0,
            'recharge_d1': None,
        }
        issues, _ = validate_record(record)
        self.assertTrue(any('点击量为0' in issue for issue in issues))

    def test_install_registration_over_100_percent_is_not_p0_rule(self):
        record = {
            'operator': 'x', 'group': 'A', 'media': 'FB', 'system': 'iOS',
            'data_type': 'yesterday', 'date': date(2026, 10, 22),
            'spend': 20, 'level1_depositors': 1, 'level1_amount': 10,
            'installs': 2, 'registrations': 3, 'recharge_d1': None,
        }
        issues, warnings = validate_record(record)
        self.assertFalse(issues)
        self.assertFalse(any('超过100%' in warning for warning in warnings))

    def test_no_spend_is_p4(self):
        rows = make_report(current={
            'spend': 0, 'level1_depositors': 0, 'first_depositors': 0,
            'level1_amount': 0, 'impressions': 0, 'clicks': 0,
            'installs': 0, 'registrations': 0, 'recharge_d1': None,
        })
        self.assertEqual(grade(rows)['auto_grade'], 'P4')

    def test_arppu_only_issue_is_structural(self):
        rows = make_report(current={
            'spend': 1000, 'level1_depositors': 40, 'first_depositors': 40,
            'level1_amount': 300, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 80, 'recharge_d1': 0.25,
        })
        for row in rows:
            if row.get('data_type') == 'daily':
                row['level1_amount'] = 300
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P2-B')

    def test_missing_retention_does_not_mark_retention_failure(self):
        rows = make_report(current={
            'spend': 1000, 'level1_depositors': 40, 'first_depositors': 40,
            'level1_amount': 500, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 80, 'recharge_d1': None,
        })
        item = grade(rows)
        self.assertNotIn('次日复充率低于配置阈值', item['grade_triggers'])
        self.assertFalse(any('次日复充率缺失' in trigger for trigger in item['grade_triggers']))

    def test_low_retention_is_structural_when_financials_are_good(self):
        rows = make_report(current={
            'spend': 1000, 'level1_depositors': 40, 'first_depositors': 40,
            'level1_amount': 500, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 80, 'recharge_d1': 0.1,
        })
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P2-B')
        self.assertIn('次日复充率低于配置阈值', item['grade_triggers'])

    def test_small_sample_single_day_cost_issue_is_p2c(self):
        rows = make_report(current={
            'spend': 150, 'level1_depositors': 3, 'first_depositors': 3,
            'level1_amount': 45, 'impressions': 1000, 'clicks': 100,
            'installs': 10, 'registrations': 8, 'recharge_d1': 0.25,
        })
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P2-C')

    def test_three_day_continuous_low_efficiency_is_p2a(self):
        rows = make_report(current={
            'spend': 500, 'level1_depositors': 10, 'first_depositors': 10,
            'level1_amount': 150, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 80, 'recharge_d1': 0.25,
        })
        for row in rows:
            if row.get('data_type') == 'daily' and row['date'] >= date(2026, 10, 19):
                row['spend'] = 1000
                row['level1_depositors'] = 20
                row['level1_amount'] = 300
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P2-A')

    def test_google_retention_does_not_use_fb_tk_floor(self):
        rows = make_report(media='Google', current={
            'spend': 1000, 'level1_depositors': 40, 'first_depositors': 40,
            'level1_amount': 500, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 80, 'recharge_d1': 0.01,
        })
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P3')
        self.assertTrue(any('未套用FB/TK' in trigger for trigger in item['grade_triggers']))

    def test_high_spend_severe_risk_overrides_small_sample(self):
        rows = make_report(current={
            'spend': 3000, 'level1_depositors': 3, 'first_depositors': 3,
            'level1_amount': 100, 'impressions': 10000, 'clicks': 1000,
            'installs': 100, 'registrations': 20, 'recharge_d1': 0.05,
        })
        item = grade(rows)
        self.assertEqual(item['auto_grade'], 'P1')
        self.assertTrue(item['needs_review'])

    def test_cpa_attribution_finds_funnel_drop_and_cpc_offset(self):
        item = {
            'current': {
                'cpc': 0.86, 'click_install_rate': 0.046,
                'install_registration_rate': 0.733,
                'registration_deposit_rate': 0.636,
                'cpa': 40.18, 'arppu': 12.22, 'roas': 0.304,
            },
            'baseline': {
                'cpc': 0.96, 'click_install_rate': 0.06,
                'install_registration_rate': 0.807,
                'registration_deposit_rate': 0.593,
                'cpa': 33.42, 'arppu': 10.26, 'roas': 0.307,
            },
            'baseline_complete': True,
        }
        result = analyze_attribution(item)
        factors = {factor['factor']: factor['contribution']
                   for factor in result['cpa_contributions']}
        self.assertLess(factors['cpc'], 0)
        self.assertGreater(factors['click_install_rate'], 0)
        self.assertGreater(factors['install_registration_rate'], 0)
        self.assertLess(factors['registration_deposit_rate'], 0)
        self.assertAlmostEqual(result['cpa_change'], 40.18 / 33.42 - 1)
        roas_factors = sum(
            factor['log_contribution'] for factor in result['roas_contributions']
        )
        self.assertAlmostEqual(
            roas_factors,
            math.log((12.22 / 40.18) / (10.26 / 33.42)),
        )

    def test_line_ids_prevent_same_operator_media_system_merging(self):
        first = make_report(line='FB-one')
        second = make_report(line='FB-two')
        reports = first + second
        assessments, _ = build_assessments(reports, CONFIG)
        self.assertEqual(len(assessments), 2)

    def test_group_filter_excludes_outsourced(self):
        rows = make_report(group='代投')
        assessments, _ = build_assessments(rows, CONFIG)
        self.assertEqual(assessments, [])

    def test_report_preserves_cost_deviation_and_grade_pivot(self):
        rows = make_report(line='FB-one')
        for row in rows:
            row['成本偏离阈值%'] = 0.125
        normalized = normalize_frame(pd.DataFrame(rows))[0]
        item = grade(normalized)
        item['effective_grade'] = 'P1'
        second = dict(item)
        second['line_id'] = 'FB-two'
        second['effective_grade'] = 'P3'

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.xlsx'
            write_workbook(output, [item, second], normalized, CONFIG)
            workbook = load_workbook(output, data_only=True)

        overview = workbook['领导速览']
        summary_headers = {cell.value: cell.column for cell in overview[2]}
        self.assertEqual(overview.cell(3, summary_headers['投手数']).value, 1)
        self.assertEqual(overview.cell(3, summary_headers['渠道数']).value, 2)
        self.assertEqual(overview.cell(3, summary_headers['P0渠道数']).value, 0)
        self.assertEqual(overview.cell(3, summary_headers['P1渠道数']).value, 1)
        self.assertEqual(overview.cell(3, summary_headers['P3渠道数']).value, 1)
        headers = {cell.value: cell.column for cell in overview[5]}
        self.assertIn('成本偏离阈值%', headers)
        self.assertEqual(overview.cell(6, headers['成本偏离阈值%']).value, 0.125)
        self.assertEqual(overview.freeze_panes, 'A6')

        trend_headers = [cell.value for cell in workbook['B组'][1]]
        self.assertIn('成本偏离阈值%', trend_headers)
        self.assertEqual(workbook['B组'].cell(2, trend_headers.index('成本偏离阈值%') + 1).value, 0.125)

    def test_group_sheets_keep_source_grade_and_conclusion_and_fill_yesterday(self):
        rows = make_report()
        normalized = normalize_frame(pd.DataFrame(rows))[0]
        normalized[1]['manual_grade'] = 'P2-B'
        normalized[1]['manual_reason'] = '历史人工结论'
        item = grade(normalized)
        item['effective_grade'] = 'P1'
        item['conclusion'] = '昨日自动结论'

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.xlsx'
            write_workbook(output, [item], normalized, CONFIG)
            workbook = load_workbook(output, data_only=True)

        sheet = workbook['B组']
        headers = {cell.value: cell.column for cell in sheet[1]}
        grade_column = headers['分级']
        conclusion_column = headers['结论']
        self.assertEqual(sheet.cell(2, grade_column).value, 'P1')
        self.assertEqual(sheet.cell(2, conclusion_column).value, '昨日自动结论')
        self.assertEqual(sheet.cell(3, grade_column).value, 'P2-B')
        self.assertEqual(sheet.cell(3, conclusion_column).value, '历史人工结论')

    def test_manual_grade_survives_same_day_rerun(self):
        rows = make_report()
        item = grade(rows)
        item['manual_grade'] = 'P2-A'
        item['manual_reason'] = '人工复核调整'
        item['effective_grade'] = 'P2-A'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            with GradeHistory(path) as history:
                history.save([item])
                overrides = history.load_overrides(item['target_date'])
                rerun = dict(item)
                rerun['auto_grade'] = 'P1'
                rerun['manual_grade'] = None
                rerun['effective_grade'] = 'P1'
                history.save([rerun])
                overrides = history.load_overrides(item['target_date'])
            self.assertEqual(overrides[assessment_key(item)]['manual_grade'], 'P2-A')
            self.assertEqual(overrides[assessment_key(item)]['manual_reason'], '人工复核调整')

    def test_manual_grade_and_conclusion_import(self):
        from openpyxl import Workbook

        from classifier import classify
        item = grade(make_report())
        item['manual_grade'] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            workbook_path = Path(directory) / 'review.xlsx'
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = '人工复核记录'
            sheet.append([
                '记录键', '日期', '人工最终分级', '人工修正原因',
                '人工最终结论', '实际处理动作', '后续效果',
            ])
            sheet.append([
                assessment_key(item), '2026-10-22', 'P2-A', '人工调整',
                '人工结论', '已控量', '次日改善',
            ])
            workbook.save(workbook_path)
            with GradeHistory(path) as history:
                history.save([item])
                self.assertEqual(history.import_reviews(workbook_path), 1)
                overrides = history.load_overrides(item['target_date'])
            saved = overrides[assessment_key(item)]
            self.assertEqual(saved['manual_grade'], 'P2-A')
            self.assertEqual(saved['manual_conclusion'], '人工结论')
            self.assertEqual(saved['actual_action'], '已控量')


if __name__ == '__main__':
    unittest.main()
