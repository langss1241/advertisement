from __future__ import annotations

import argparse
import  cawl_google
import withdraw
from datetime import date
import json
from pathlib import Path

import pandas as pd

from attribution import analyze_attribution
from backtest import run_historical_backtest
from classifier import classify
from conclusion import make_conclusion, recommendation
from data_loader import load_frame, load_report, normalize_frame
from history import GradeHistory, assessment_key
from metrics import build_assessments, derive_metrics
from report import write_backtest_diff, write_workbook



BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.json'


def _load_config(path: str | Path = CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _load_database_report() -> tuple[list[dict], list[str]]:
    try:
        from withdraw import query_local_database
    except ImportError as exc:
        raise RuntimeError(
            'Database mode requires the existing withdraw.py and psycopg dependency; '
            'use --input with a CSV/XLSX export instead.'
        ) from exc
    return normalize_frame(query_local_database())


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _known_case_check(assessments: list[dict]) -> dict:
    candidates = [
        item for item in assessments
        if item.get('operator') == '猴子'
        and item.get('group') == 'B'
        and item.get('media', '').upper() == 'FB'
        and item.get('system', '').casefold() == 'ios'
    ]
    aggregate_spend = sum(
        item.get('spend') or 0 for item in candidates
    )
    spend_matches = bool(candidates) and abs(aggregate_spend - 2812.53) < 0.01
    return {
        'case': '猴子｜B组｜FB iOS｜2026-10-22',
        'expected_spend_usd': 2812.53,
        'candidate_line_count': len(candidates),
        'aggregate_spend_usd': aggregate_spend if candidates else None,
        'aggregate_spend_matches': spend_matches,
        'status': (
            'aggregate spend matches; provide benchmark labels to validate all attribution metrics'
            if spend_matches else
            'aggregate case not found in current report'
        ),
        'observed_line_spends_usd': [item.get('spend') for item in candidates],
    }


def run(
    input_path: str | None = None,
    output_dir: str | Path | None = None,
    history_path: str | Path | None = None,
    target_date: date | None = None,
    import_review: str | None = None,
    benchmark_labels: str | None = None,
) -> dict:


    cawl_google.main()
    withdraw.main()
    config = _load_config()
    report_dir = Path(output_dir) if output_dir else BASE_DIR / config['report']['output_dir']
    db_path = Path(history_path) if history_path else BASE_DIR / config['report']['history_db']
    if not report_dir.is_absolute():
        report_dir = BASE_DIR / report_dir
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    history = GradeHistory(db_path)
    try:
        if import_review:
            count = history.import_reviews(import_review)
            print(f'Imported {count} manual review(s).')

        records, _ = load_report(input_path) if input_path else _load_database_report()
        assessments, eligible_records = build_assessments(records, config, target_date)
        if not assessments:
            raise ValueError(
                'No ABC yesterday lines found. Confirm the report date and A/B/C group mapping.'
            )
        date_key = assessments[0]['target_date']
        overrides = history.load_overrides(date_key)
        previous = history.load_previous(date_key)
        grade_rank = {'P0': 0, 'P1': 1, 'P2-A': 2, 'P2-B': 3,
                      'P2-C': 4, 'P3': 5, 'P4': 6}
        for item in assessments:
            key = assessment_key(item)
            saved = overrides.get(key, {})
            prior = previous.get(key, {})
            item['manual_grade'] = item.get('manual_grade') or saved.get('manual_grade')
            item['manual_reason'] = item.get('manual_reason') or saved.get('manual_reason')
            item['manual_conclusion'] = saved.get('manual_conclusion')
            item['actual_action'] = saved.get('actual_action')
            item['follow_up'] = saved.get('follow_up')
            item['previous_grade'] = prior.get('previous_grade')
            item['previous_abnormal_days'] = prior.get('abnormal_days', 0)
            item['attribution'] = analyze_attribution(item)
            classify(item, config)
            current_grade = item['auto_grade']
            prior_grade = item['previous_grade']
            item['new_anomaly'] = current_grade in {
                'P0', 'P1', 'P2-A', 'P2-B', 'P2-C'
            } and prior_grade not in {'P0', 'P1', 'P2-A', 'P2-B', 'P2-C'}
            if prior_grade is None:
                item['grade_change'] = '新增记录'
            elif grade_rank[current_grade] < grade_rank[prior_grade]:
                item['grade_change'] = '风险升级'
            elif grade_rank[current_grade] > grade_rank[prior_grade]:
                item['grade_change'] = '风险降级'
            else:
                item['grade_change'] = '分级不变'
            item['recommendation'] = recommendation(item)
            item['conclusion'] = make_conclusion(item)
        history.save(assessments)

        for record in eligible_records:
            record['derived'] = derive_metrics(record)

        stamp = date_key.strftime('%Y%m%d')
        workbook = report_dir / f'投放日报_分级结论_{stamp}.xlsx'
        diff_report = report_dir / f'人工分级差异报告_{stamp}.csv'
        summary_path = report_dir / f'回测摘要_{stamp}.json'
        write_workbook(workbook, assessments, eligible_records, config)
        backtest = write_backtest_diff(diff_report, assessments)
        historical_backtest = None
        if benchmark_labels:
            manual_records, _ = normalize_frame(load_frame(benchmark_labels))
            historical_diff = report_dir / f'历史人工回测差异_{stamp}.csv'
            historical_backtest = run_historical_backtest(
                records, manual_records, config, historical_diff
            )
        summary = {
            'report_date': date_key.isoformat(),
            'eligible_line_count': len(assessments),
            'labelled_line_count': (
                historical_backtest['manual_label_count']
                if historical_backtest else backtest['labelled']
            ),
            'unlabelled_line_count': (
                historical_backtest['unmatched_count']
                if historical_backtest else backtest['unlabelled']
            ),
            'agreement_rate': (
                historical_backtest['overall_agreement']
                if historical_backtest else backtest['agreement']
            ),
            'manual_metrics_status': (
                'computed at historical composite-key grain'
                if historical_backtest else
                'not computable: no manual grades provided'
                if backtest['labelled'] == 0 else
                'computed for labelled rows only'
            ),
            'known_case_check': (
                historical_backtest['known_case_check']
                if historical_backtest else _known_case_check(assessments)
            ),
            'historical_backtest': historical_backtest,
            'files': {
                'workbook': str(workbook),
                'manual_diff_report': str(diff_report),
                'historical_backtest_diff': (
                    historical_backtest['difference_csv'] if historical_backtest else None
                ),
            },
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return summary
    finally:
        history.close()


def main():
    parser = argparse.ArgumentParser(
        description='ABC投放日报自动分级、初步归因及人工复核'
    )
    parser.add_argument('--input', help='现有日报 CSV/XLSX；不提供时读取 withdraw.py 查询结果')
    parser.add_argument('--date', help='指定报表日期，格式 YYYY-MM-DD')
    parser.add_argument('--output-dir', help='输出目录，默认 reports')
    parser.add_argument('--history-db', help='历史人工分级 SQLite 文件路径')
    parser.add_argument('--import-review', help='导入人工复核记录工作簿中的人工最终分级和备注')
    parser.add_argument('--benchmark-labels', help='用于历史回测的人工分级 CSV/XLSX')
    args = parser.parse_args()
    result = run(
        input_path=args.input,
        output_dir=args.output_dir,
        history_path=args.history_db,
        target_date=_parse_date(args.date),
        import_review=args.import_review,
        benchmark_labels=args.benchmark_labels,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
