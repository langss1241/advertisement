#!/usr/bin/env python3
import pandas as pd
import psycopg
from psycopg.rows import dict_row



yesterday = (pd.Timestamp.now().normalize() - pd.Timedelta(days=4)).date()   # -4
start_date = (pd.Timestamp.now().normalize() - pd.Timedelta(days=11)).date()


QUERY = """
WITH params AS (
    SELECT
        %s::date AS target_date,
        %s::date AS start_date
),
base_data AS (
    SELECT
        a."date",
        a.source_operator,
        a.media,
        a.placement_type,
        a.channel,
        SUM(a.actual_spend_usd) AS actual_spend_usd,
        SUM(a.impressions) AS impressions,
        SUM(a.clicks) AS clicks,
        SUM(a.installs) AS installs,
        SUM(a.level1_registrations) AS level1_registrations,
        SUM(a.level1_first_depositors) AS level1_first_depositors,
        SUM(a.level1_first_deposit_amount) AS level1_first_deposit_amount,
        SUM(a.first_depositors) AS first_depositors,
        SUM(a.level1_first_deposit_amount)
            / NULLIF(SUM(a.level1_first_depositors), 0)
            AS level1_first_deposit_arppu,
        SUM(a.level1_first_deposit_amount)
            / NULLIF(SUM(a.actual_spend_usd), 0)
            AS level1_roas,
        AVG(a.recharge_d1) AS recharge_d1,
        AVG(a.recharge_d3) AS recharge_d3,
        AVG(a.recharge_d7) AS recharge_d7
    FROM public.ads_daily_raw a
    CROSS JOIN params p
    WHERE a."date" BETWEEN p.start_date AND p.target_date
    GROUP BY
        a."date",
        a.source_operator,
        a.media,
        a.placement_type,
        a.channel
),
raw_data AS (
    SELECT
        b.*,
        CASE
            WHEN b.placement_type ILIKE '%%ios%%'
              OR b.placement_type ILIKE '%%苹果%%'
              OR b.placement_type ILIKE '%%iphone%%'
                THEN 'iOS'
            WHEN b.placement_type ILIKE '%%安卓%%'
              OR b.placement_type ILIKE '%%android%%'
                THEN '安卓'
            WHEN b.placement_type ILIKE '%%PWA%%'
            THEN 'PWA'
            WHEN b.placement_type ILIKE '%%H5%%'
                THEN 'H5'
            ELSE COALESCE(b.placement_type, '未知')
        END AS os_type
    FROM base_data b
),
mapped_data AS (
    SELECT
        r.*,
        COALESCE(ai.operator_group, '未分组') AS group_name
    FROM raw_data r
    LEFT JOIN public.agent_id ai        
        ON ai.source_operator = r.source_operator

),
daily AS (
    SELECT
        "date",
        source_operator AS operator_name,
        channel,
        group_name,
        media,
        os_type,
        SUM(actual_spend_usd) AS spend,
        SUM(impressions) AS impressions,
        SUM(clicks) AS clicks,
        SUM(installs) AS installs,
        SUM(level1_registrations) AS registrations,
        SUM(level1_first_depositors) AS level1_depositors,
        SUM(level1_first_deposit_amount) AS level1_amount,
        SUM(first_depositors) AS total_first_depositors,
        AVG(recharge_d1) AS recharge_d1,
        AVG(recharge_d3) AS recharge_d3,
        AVG(recharge_d7) AS recharge_d7
    FROM mapped_data
    GROUP BY
        "date",
        source_operator,
        channel,
        group_name,
        media,
        os_type
),
yesterday AS (
    SELECT
        '昨日'::text AS data_type,
        d.*
    FROM daily d
    CROSS JOIN params p
    WHERE d."date" = p.target_date
),
previous_7_days AS (
    SELECT
        '前7日明细'::text AS data_type,
        d.*
    FROM daily d
    CROSS JOIN params p
    WHERE d."date" >= p.start_date
      AND d."date" < p.target_date
),
previous_7_avg AS (
    SELECT
        '前7日均值'::text AS data_type,
        NULL::date AS "date",
        operator_name,
        channel,
        group_name,
        media,
        os_type,
        SUM(spend) / 7.0 AS spend,
        SUM(impressions) / 7.0 AS impressions,
        SUM(clicks) / 7.0 AS clicks,
        SUM(installs) / 7.0 AS installs,
        SUM(registrations) / 7.0 AS registrations,
        SUM(level1_depositors) / 7.0 AS level1_depositors,
        SUM(level1_amount) / 7.0 AS level1_amount,
        SUM(total_first_depositors) / 7.0 AS total_first_depositors,
        AVG(recharge_d1) AS recharge_d1,
        AVG(recharge_d3) AS recharge_d3,
        AVG(recharge_d7) AS recharge_d7
    FROM previous_7_days
    GROUP BY
        operator_name,
        channel,
        group_name,
        media,
        os_type
),
combined AS (
    SELECT * FROM yesterday
    UNION ALL
    SELECT * FROM previous_7_days
    UNION ALL
    SELECT * FROM previous_7_avg
),
metrics AS (
    SELECT
        c.*,
        CASE
            WHEN os_type = '安卓' THEN 25::numeric
            WHEN os_type = 'PWA' THEN 25::numeric
            WHEN os_type = 'H5' THEN 25::numeric
            WHEN os_type = 'iOS' THEN 35::numeric
            ELSE NULL::numeric
        END AS cost_threshold,
        spend / NULLIF(impressions, 0) * 1000 AS calc_cpm,
        clicks / NULLIF(impressions, 0) AS calc_ctr,
        spend / NULLIF(clicks, 0) AS calc_cpc,
        installs / NULLIF(clicks, 0) AS click_install_rate,
        registrations / NULLIF(installs, 0) AS install_registration_rate,
        level1_depositors / NULLIF(registrations, 0)
            AS registration_deposit_rate,
        spend / NULLIF(level1_depositors, 0) AS level1_cost,
        level1_amount / NULLIF(spend, 0) AS level1_roas,
        level1_amount / NULLIF(level1_depositors, 0) AS level1_arppu
    FROM combined c
)
SELECT
    data_type AS "数据类型",
    "date" AS "日期",
    operator_name AS "投手",
    channel AS "渠道",
    group_name AS "组别",
    media AS "媒体",
    os_type AS "系统",
    ROUND(spend, 2) AS "实际消耗$",
    ROUND(level1_depositors, 2) AS "一级首充人数",
    ROUND(level1_cost, 2) AS "一级首充成本$",
    ROUND(level1_cost / NULLIF(cost_threshold, 0) - 1, 4)
        AS "成本偏离阈值%%",
    ROUND(level1_roas, 4) AS "一级ROAS",
    ROUND(level1_arppu, 2) AS "一级ARPPU$",
    ROUND(total_first_depositors, 2) AS "首充人数",
    ROUND(recharge_d1, 4) AS "次日复充率",
    ROUND(recharge_d3, 4) AS "3日复充率",
    ROUND(recharge_d7, 4) AS "7日复充率",
    ROUND(impressions, 2) AS "曝光量",
    ROUND(clicks, 2) AS "点击量",
    ROUND(installs, 2) AS "安装量",
    ROUND(registrations, 2) AS "一级注册人数",
    ROUND(level1_amount, 2) AS "一级首充金额$",
    ROUND(calc_cpm, 2) AS "CPM$",
    ROUND(calc_ctr, 4) AS "CTR",
    ROUND(calc_cpc, 2) AS "CPC$",
    ROUND(click_install_rate, 4) AS "点击安装率",
    ROUND(install_registration_rate, 4) AS "安装注册率",
    ROUND(registration_deposit_rate, 4) AS "一级注册首充率"
FROM metrics
ORDER BY
    "投手",
    "渠道",
    "组别",
    "媒体",
    "系统",

    CASE data_type
        WHEN '昨日' THEN 0
        WHEN '前7日均值' THEN 1
        ELSE 2
    END,
    "日期" DESC NULLS LAST
"""


def query_local_database():
    """执行报表 SQL，返回 pandas.DataFrame。"""
    with psycopg.connect(
        host='localhost',
        port=5432,
        dbname='postgres',
        user='postgres',
        password='147258',
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(QUERY, (yesterday, start_date))
            return pd.DataFrame(cursor.fetchall())

        
if __name__ == '__main__':
    df = query_local_database()
    df.to_csv(f'ads_daily_report_{yesterday.strftime("%m%d")}.csv', index=False, encoding='utf-8-sig')
