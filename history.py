from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import sqlite3


def assessment_key(assessment: dict) -> str:
    composite = '|'.join(str(assessment.get(field) or '') for field in
                         ('group', 'operator', 'media', 'system'))
    if assessment.get('line_id_source') == 'composite':
        return composite
    return '|'.join(str(assessment.get(field) or '') for field in
                    ('group', 'line_id', 'operator', 'media', 'system'))


class GradeHistory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS grade_history (
                report_date TEXT NOT NULL,
                record_key TEXT NOT NULL,
                line_id TEXT NOT NULL,
                group_name TEXT NOT NULL,
                operator_name TEXT NOT NULL,
                media TEXT NOT NULL,
                system TEXT NOT NULL,
                auto_grade TEXT NOT NULL,
                manual_grade TEXT,
                effective_grade TEXT NOT NULL,
                previous_grade TEXT,
                abnormal_days INTEGER NOT NULL DEFAULT 0,
                new_anomaly INTEGER NOT NULL DEFAULT 0,
                grade_change TEXT,
                triggers_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                risk_score REAL NOT NULL,
                attribution_json TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                manual_reason TEXT,
                manual_conclusion TEXT,
                actual_action TEXT,
                follow_up TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (report_date, record_key)
            )
        """)
        existing_columns = {
            row['name'] for row in self.connection.execute(
                'PRAGMA table_info(grade_history)'
            )
        }
        if 'manual_conclusion' not in existing_columns:
            self.connection.execute(
                'ALTER TABLE grade_history ADD COLUMN manual_conclusion TEXT'
            )
        for column, definition in (
            ('previous_grade', 'TEXT'),
            ('abnormal_days', 'INTEGER NOT NULL DEFAULT 0'),
            ('new_anomaly', 'INTEGER NOT NULL DEFAULT 0'),
            ('grade_change', 'TEXT'),
        ):
            if column not in existing_columns:
                self.connection.execute(
                    f'ALTER TABLE grade_history ADD COLUMN {column} {definition}'
                )
        self.connection.commit()

    def load_overrides(self, report_date: date) -> dict[str, dict]:
        rows = self.connection.execute(
            """SELECT record_key, manual_grade, manual_reason, manual_conclusion,
                      actual_action, follow_up
               FROM grade_history
               WHERE report_date = ? AND (
                   manual_grade IS NOT NULL OR manual_reason IS NOT NULL
                   OR manual_conclusion IS NOT NULL OR actual_action IS NOT NULL
                   OR follow_up IS NOT NULL
               )""",
            (report_date.isoformat(),),
        ).fetchall()
        return {
            row['record_key']: {
                'manual_grade': row['manual_grade'],
                'manual_reason': row['manual_reason'],
                'manual_conclusion': row['manual_conclusion'],
                'actual_action': row['actual_action'],
                'follow_up': row['follow_up'],
            }
            for row in rows
        }

    def load_previous(self, report_date: date) -> dict[str, dict]:
        rows = self.connection.execute("""
            SELECT record_key, effective_grade, abnormal_days
            FROM grade_history
            WHERE report_date < ?
            ORDER BY report_date DESC
        """, (report_date.isoformat(),)).fetchall()
        previous = {}
        for row in rows:
            previous.setdefault(row['record_key'], {
                'previous_grade': row['effective_grade'],
                'abnormal_days': row['abnormal_days'],
            })
        return previous

    def save(self, assessments: list[dict]) -> None:
        now = datetime.now().isoformat(timespec='seconds')
        rows = []
        for item in assessments:
            target = item['target_date'].isoformat()
            record_key = assessment_key(item)
            manual_grade = item.get('manual_grade')
            manual_reason = item.get('manual_reason')
            rows.append((
                target, record_key, str(item.get('line_id') or ''), item.get('group') or '',
                item.get('operator') or '', item.get('media') or '', item.get('system') or '',
                item['auto_grade'], manual_grade, item['effective_grade'],
                item.get('previous_grade'), item.get('abnormal_days', 0),
                int(bool(item.get('new_anomaly'))), item.get('grade_change'),
                json.dumps(item.get('grade_triggers', []), ensure_ascii=False),
                item['confidence'], item['risk_score'],
                json.dumps(item.get('attribution', {}), ensure_ascii=False),
                item.get('recommendation', ''), manual_reason,
                item.get('manual_conclusion'), item.get('actual_action'),
                item.get('follow_up'), now,
            ))
        self.connection.executemany("""
            INSERT INTO grade_history (
                report_date, record_key, line_id, group_name, operator_name,
                media, system, auto_grade, manual_grade, effective_grade,
                previous_grade, abnormal_days, new_anomaly, grade_change,
                triggers_json, confidence, risk_score, attribution_json,
                recommendation, manual_reason, manual_conclusion, actual_action,
                follow_up, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_date, record_key) DO UPDATE SET
                line_id=excluded.line_id, group_name=excluded.group_name,
                operator_name=excluded.operator_name, media=excluded.media,
                system=excluded.system, auto_grade=excluded.auto_grade,
                previous_grade=excluded.previous_grade,
                abnormal_days=excluded.abnormal_days,
                new_anomaly=excluded.new_anomaly,
                grade_change=excluded.grade_change,
                effective_grade=CASE
                    WHEN grade_history.manual_grade IS NOT NULL
                    THEN grade_history.manual_grade ELSE excluded.auto_grade END,
                triggers_json=excluded.triggers_json,
                confidence=excluded.confidence, risk_score=excluded.risk_score,
                attribution_json=excluded.attribution_json,
                recommendation=excluded.recommendation, updated_at=excluded.updated_at
        """, rows)
        self.connection.commit()

    def import_reviews(self, workbook: str | Path) -> int:
        from openpyxl import load_workbook

        book = load_workbook(workbook, read_only=True, data_only=True)
        if '人工复核记录' not in book.sheetnames:
            raise ValueError('Workbook has no 人工复核记录 sheet')
        sheet = book['人工复核记录']
        values = sheet.iter_rows(values_only=True)
        headers = next(values, ())
        index = {str(value).strip(): position for position, value in enumerate(headers)
                 if value is not None}
        required = ('记录键', '日期', '人工最终分级')
        if any(name not in index for name in required):
            raise ValueError('人工复核记录 sheet 缺少记录键、日期或人工最终分级列')
        allowed = {'P0', 'P1', 'P2-A', 'P2-B', 'P2-C', 'P3', 'P4'}
        count = 0
        for row in values:
            key = row[index['记录键']]
            report_date = row[index['日期']]
            grade = row[index['人工最终分级']]
            reason = row[index['人工修正原因']] if '人工修正原因' in index else None
            conclusion = row[index['人工最终结论']] if '人工最终结论' in index else None
            action = row[index['实际处理动作']] if '实际处理动作' in index else None
            follow_up = row[index['后续效果']] if '后续效果' in index else None
            if not key or not report_date:
                continue
            grade = str(grade).strip().upper() if grade is not None else None
            if grade and grade not in allowed:
                raise ValueError(f'Unknown manual grade: {grade}')
            if not any((grade, reason, conclusion, action, follow_up)):
                continue
            cursor = self.connection.execute("""
                UPDATE grade_history
                SET manual_grade=COALESCE(?, manual_grade),
                    effective_grade=COALESCE(?, effective_grade),
                    manual_reason=?,
                    manual_conclusion=?, actual_action=?, follow_up=?, updated_at=?
                WHERE report_date=? AND record_key=?
            """, (grade, grade, reason, conclusion, action, follow_up,
                  datetime.now().isoformat(timespec='seconds'),
                  str(report_date)[:10], str(key)))
            if cursor.rowcount == 0:
                raise ValueError(f'No saved history row matches {report_date}: {key}')
            count += 1
        self.connection.commit()
        book.close()
        return count

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
