import os
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta

APP_PASSWORD = "ANZDRP2026"

st.set_page_config(
    page_title="DRP System - IMCD ANZ",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

@st.cache_resource
def get_connection():
    return st.connection("snowflake")

DRP_SQL = """
-- Combined DRP V4 query: ITEMBP as base, with pre-aggregated STOCK and SO joined
-- Co-authored with CoCo
WITH STOCK_AGG AS (
    SELECT
        COMPANY_COUNTRY,
        WAREHOUSE_ERP_NUMBER,
        PRODUCT_FULL_SEGMENTS_NUMBER,
        BRANCHPLANT,
        AVG(UNITCOST_LOCAL) AS UNITCOST,
        SUM(ONHANDQTY) AS ONHANDQTY,
        SUM(EXPIREDQTY) AS EXPIREDQTY,
        SUM(BLOCKEDQTY) AS BLOCKEDQTY,
        SUM(INTRANSITQTY) AS INTRANSITQTY,
        SUM(ONPO) AS ONPOQTY,
        SUM(SOSOFTCOMMIT) AS SOSOFTCOMMIT,
        SUM(SOHARDCOMMIT) AS SOHARDCOMMIT,
        SUM(WOSOFTCOMMIT) AS WOSOFTCOMMIT,
        SUM(WOHARDCOMMIT) AS WOHARDCOMMIT,
        SUM(ONWO) AS ONWO,
        SUM(QUANTITY_ON_SO_OTHER_1) AS SOOTHER,
        SUM(QUANTITY_ON_PO_OTHER_1) AS POOTHER,
        SUM(SOSOFTCOMMIT + SOHARDCOMMIT + WOSOFTCOMMIT + WOHARDCOMMIT + ONWO + QUANTITY_ON_SO_OTHER_1) AS TOTALDEMAND,
        SUM(INTRANSITQTY + ONPO + QUANTITY_ON_PO_OTHER_1) AS SUPPLYOP
    FROM PROD_ENT_DWH_DB.ANALYTICS.STOCK
    WHERE BRANCHPLANT NOT IN ('61100','62100')
    GROUP BY COMPANY_COUNTRY, WAREHOUSE_ERP_NUMBER, PRODUCT_FULL_SEGMENTS_NUMBER, BRANCHPLANT
),

DEMAND_AGG AS (
    SELECT
        BRANCHPLANT,
        PRODUCT_FULL_SEGMENTS_NUMBER,
        SUM(CASE
            WHEN ORDERTYPECODE IN ('SO','ST')
                 AND CAST(REGEXP_SUBSTR(NEXTSTATUS, '[0-9]+') AS DECIMAL(38,0)) > 524
                 AND REPORTINGDATE <= DATEADD(DAY, LEADTIME_LEVEL + 30, CURRENT_DATE)
            THEN QUANTITYORDERED - QUANTITYSHIPPED END) AS Q_SO,
        SUM(CASE
            WHEN ORDERTYPECODE IN ('SO','ST')
                 AND CAST(REGEXP_SUBSTR(NEXTSTATUS, '[0-9]+') AS DECIMAL(38,0)) > 524
                 AND REPORTINGDATE <= DATEADD(DAY, 30, CURRENT_DATE)
            THEN QUANTITYORDERED - QUANTITYSHIPPED END) AS N30D_SO,
        SUM(CASE
            WHEN ORDERTYPECODE IN ('SB')
                 AND CAST(REGEXP_SUBSTR(NEXTSTATUS, '[0-9]+') AS DECIMAL(38,0)) > 524
                 AND REQUESTEDDATE <= DATEADD(DAY, LEADTIME_LEVEL + 30, CURRENT_DATE)
            THEN QUANTITYORDERED - QUANTITYSHIPPED END) AS Q_SB,
        SUM(QUANTITYORDERED - QUANTITYSHIPPED) AS TOTAL_DEMAND
    FROM PROD_ENT_DWH_DB.ANALYTICS.SALESBUDGET
    WHERE
        ORDERTYPECODE IN ('SO','ST','SB')
        AND ORDERSTATUS NOT IN ('CANCELLED','CLOSED')
        AND PRODUCT_RECORD_TYPE = 'Product'
        AND CAST(REGEXP_SUBSTR(NEXTSTATUS, '[0-9]+') AS DECIMAL(38,0)) > 524
    GROUP BY BRANCHPLANT, PRODUCT_FULL_SEGMENTS_NUMBER
),

FADU_COUNTRY AS (
    SELECT
        CASE WHEN COMPANY_NUMBER = '61100' THEN 'Australia' ELSE 'New Zealand' END AS COMPANY_COUNTRY,
        PRODUCT_FULL_SEGMENTS_NUMBER,
        ROUND(SUM(FORECAST_QUANTITY) / 365, 2) AS AVG_NEXT_12M,
        ROUND(SUM(CASE WHEN TIMESTAMP < DATEADD(MONTH, 6, CURRENT_DATE) THEN FORECAST_QUANTITY END) / 180, 2) AS AVG_NEXT_6M,
        ROUND(SUM(CASE WHEN TIMESTAMP < DATEADD(MONTH, 3, CURRENT_DATE) THEN FORECAST_QUANTITY END) / 90, 2) AS AVG_NEXT_3M,
        ROUND((AVG_NEXT_12M + AVG_NEXT_6M + AVG_NEXT_3M) / 3, 2) AS FCST_ADU
    FROM PROD_ENT_DWH_DB.ANALYTICS.SALESFORECAST_REPORTING
    WHERE
        COMPANY_NUMBER IN ('61100','62100')
        AND (METHOD IS NULL OR METHOD = 'Stock')
        AND CURRENT_FLAG = 'TRUE'
        AND TIMESTAMP > CURRENT_DATE
    GROUP BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER
),

HADU_AGG AS (
    SELECT
        COMPANY_COUNTRY,
        PRODUCT_FULL_SEGMENTS_NUMBER,
        BRANCHPLANT,
        ROUND(GREATEST(SUM(QUANTITYORDERED) / 365, 0), 2) AS AVG_L12M,
        ROUND(GREATEST(SUM(CASE WHEN REPORTINGDATE >= DATE_TRUNC('MONTH', DATEADD(MONTH, -6, CURRENT_DATE)) THEN QUANTITYORDERED END) / 180, 0), 2) AS AVG_L6M,
        ROUND(GREATEST(SUM(CASE WHEN REPORTINGDATE >= DATE_TRUNC('MONTH', DATEADD(MONTH, -3, CURRENT_DATE)) THEN QUANTITYORDERED END) / 90, 0), 2) AS AVG_L3M,
        ROUND(GREATEST((COALESCE(AVG_L12M,0) + COALESCE(AVG_L6M,0) + COALESCE(AVG_L3M,0)) / 3, 0), 2) AS L12MADU
    FROM PROD_ENT_DWH_DB.ANALYTICS.SALESBUDGET
    WHERE REPORTINGDATE BETWEEN DATE_TRUNC('MONTH', DATEADD(MONTH, -12, CURRENT_DATE))
                           AND LAST_DAY(DATEADD(MONTH, -1, CURRENT_DATE))
      AND ORDERTYPECODE = 'SO'
      AND SHIP_TO_NAME NOT LIKE '%IMCD%'
      AND PRODUCT_RECORD_TYPE = 'Product'
      AND ORDERSTATUS NOT IN ('CANCELLED')
      AND ACCOUNT_STATUS = 'Active'
    GROUP BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER, BRANCHPLANT
),

HADU_STATE AS (
    SELECT
        COMPANY_COUNTRY,
        PRODUCT_FULL_SEGMENTS_NUMBER,
        STATE,
        SUM(DISTRIBUTED_ADU) AS H_ADU
    FROM (
        SELECT
            S.COMPANY_COUNTRY,
            SO.PRODUCT_FULL_SEGMENTS_NUMBER,
            CASE WHEN A.ACCOUNT_ADDRESS_STATE = ' ' THEN 'NZ' ELSE A.ACCOUNT_ADDRESS_STATE END AS STATE,
            ROUND(SO.L12MADU / COUNT(*) OVER (PARTITION BY SO.PRODUCT_FULL_SEGMENTS_NUMBER, SO.BRANCHPLANT), 2) AS DISTRIBUTED_ADU
        FROM HADU_AGG SO
        INNER JOIN STOCK_AGG S
            ON SO.PRODUCT_FULL_SEGMENTS_NUMBER = S.PRODUCT_FULL_SEGMENTS_NUMBER
            AND SO.BRANCHPLANT = S.BRANCHPLANT
        LEFT JOIN PROD_ENT_DWH_DB.ANALYTICS.ACCOUNTS A
            ON S.WAREHOUSE_ERP_NUMBER = A.ACCOUNT_ERP_NUMBER
    )
    GROUP BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER, STATE
),

FINAL AS (
    SELECT
        I.PRODUCT_FULL_SEGMENTS_NUMBER,
        I.PRODUCT_NAME,
        I.PRODUCT_PACKAGE_DESCRIPTION,
        I.ITEM_NET_WEIGHT,
        S.COMPANY_COUNTRY,
        CASE WHEN A.ACCOUNT_ADDRESS_STATE = ' ' THEN 'NZ' ELSE A.ACCOUNT_ADDRESS_STATE END AS STATE,
        SUM(S.ONHANDQTY) AS ONHANDQTY,
        SUM(S.EXPIREDQTY) AS EXPIREDQTY,
        SUM(S.BLOCKEDQTY) AS BLOCKEDQTY,
        SUM(S.INTRANSITQTY) AS INTRANSITQTY,
        SUM(S.ONPOQTY) AS ONPOQTY,
        SUM(S.SOOTHER) AS SOOTHER,
        SUM(S.POOTHER) AS POOTHER,
        SUM(S.SUPPLYOP) AS SUPPLYOP,
        SUM(COALESCE(D.Q_SO, 0)) AS Q_SO,
        SUM(COALESCE(D.Q_SB, 0)) AS Q_SB,
        SUM(COALESCE(D.N30D_SO, 0)) AS N30D_SO,
        SUM(COALESCE(D.TOTAL_DEMAND, 0)) AS TOTAL_DEMAND,
        LISTAGG(DISTINCT IFNULL(I.WAREHOUSE_PROCESS_GROUP_3_CODE, 'NULL'), ', ') AS ITEM_GROUP,
        LISTAGG(DISTINCT IFNULL(I.STOCKINGTYPE, 'NULL'), ', ') AS STOCKINGTYPE,
        LISTAGG(DISTINCT IFNULL(I.INVENTORYTYPE, 'NULL'), ', ') AS INVENTORYTYPE,
        LISTAGG(DISTINCT IFNULL(I.VENDOR_NAME, 'NULL'), ', ') AS VENDOR_NAME,
        LISTAGG(DISTINCT IFNULL(I.LOCAL_PRODUCTMANAGER_NAME, 'NULL'), ', ') AS LOCAL_PRODUCTMANAGER_NAME,
        LISTAGG(DISTINCT IFNULL(I.PRODUCT_PRINCIPAL_NAME, 'NULL'), ', ') AS PRODUCT_PRINCIPAL_NAME,
        LISTAGG(DISTINCT IFNULL(I.INVENTORY_OWNERSHIP, 'NULL'), ', ') AS INVENTORY_OWNERSHIP,
        LISTAGG(DISTINCT IFNULL(I.BUYER_NAME, 'NULL'), ', ') AS BUYER_NAME,
        LISTAGG(DISTINCT IFNULL(I.COUNTRY_OF_ORIGIN, 'NULL'), ', ') AS COO,
        LISTAGG(DISTINCT IFNULL(I.LEADTIMELEVEL, 'NULL'), ', ') AS LEADTIME,
        LISTAGG(DISTINCT IFNULL(I.SHELFLIFEDAYS, 'NULL'), ', ') AS SHELFLIFE,
        MAX(CASE WHEN I.INVENTORYTYPE LIKE '%A%' OR I.INVENTORYTYPE LIKE '%B%' OR I.INVENTORYTYPE LIKE '%M%' THEN 30 ELSE 0 END) AS MIN_DAYS,
        MAX(CASE WHEN I.INVENTORYTYPE LIKE '%A%' OR I.INVENTORYTYPE LIKE '%B%' OR I.INVENTORYTYPE LIKE '%M%' THEN 60 ELSE 0 END) AS MAX_DAYS,
        MAX(CASE WHEN I.INVENTORYTYPE LIKE '%A%' OR I.INVENTORYTYPE LIKE '%B%' OR I.INVENTORYTYPE LIKE '%M%' THEN I.LEADTIMELEVEL + 60 ELSE 0 END) AS IDEAL_PIPELINE,
        MAX(I.LEADTIMELEVEL) AS LEADTIMELEVEL,
        MAX(I.SHELFLIFEDAYS) AS SHELFLIFEDAYS,
        MAX(I.MINREORDERQTY) AS MOQ,
        MAX(I.PALLET_SIZE) AS PALLET_SIZE,
        MAX(I.UNITSPERCONTAINER) AS UNITSPERCONTAINER,
        MAX(COALESCE(HS.H_ADU, 0)) AS H_ADU,
        MAX(COALESCE(FC.FCST_ADU, 0)) AS FCST_TOTAL,
        ROUND(AVG(S.UNITCOST), 2) AS UNITCOST
    FROM
        PROD_ENT_DWH_DB.ANALYTICS.ITEMBP AS I
        LEFT JOIN STOCK_AGG AS S
            ON I.PRODUCT_FULL_SEGMENTS_NUMBER = S.PRODUCT_FULL_SEGMENTS_NUMBER
            AND I.BRANCHPLANT = S.BRANCHPLANT
        LEFT JOIN PROD_ENT_DWH_DB.ANALYTICS.ACCOUNTS AS A
            ON S.WAREHOUSE_ERP_NUMBER = A.ACCOUNT_ERP_NUMBER
        LEFT JOIN HADU_STATE AS HS
            ON S.COMPANY_COUNTRY = HS.COMPANY_COUNTRY
            AND I.PRODUCT_FULL_SEGMENTS_NUMBER = HS.PRODUCT_FULL_SEGMENTS_NUMBER
            AND CASE WHEN A.ACCOUNT_ADDRESS_STATE = ' ' THEN 'NZ' ELSE A.ACCOUNT_ADDRESS_STATE END = HS.STATE
        LEFT JOIN FADU_COUNTRY AS FC
            ON I.PRODUCT_FULL_SEGMENTS_NUMBER = FC.PRODUCT_FULL_SEGMENTS_NUMBER
            AND S.COMPANY_COUNTRY = FC.COMPANY_COUNTRY
        LEFT JOIN DEMAND_AGG AS D
            ON I.PRODUCT_FULL_SEGMENTS_NUMBER = D.PRODUCT_FULL_SEGMENTS_NUMBER
            AND I.BRANCHPLANT = D.BRANCHPLANT
    WHERE
        I.BRANCHPLANT NOT IN ('61100','62100')
        AND I.STOCKINGTYPE IN ('S','U')
        AND I.PRODUCT_RECORD_TYPE = 'Product'
    GROUP BY
        I.PRODUCT_FULL_SEGMENTS_NUMBER,
        I.PRODUCT_NAME,
        I.PRODUCT_PACKAGE_DESCRIPTION,
        I.ITEM_NET_WEIGHT,
        S.COMPANY_COUNTRY,
        CASE WHEN A.ACCOUNT_ADDRESS_STATE = ' ' THEN 'NZ' ELSE A.ACCOUNT_ADDRESS_STATE END
    HAVING NOT (
        SUM(S.ONHANDQTY) = 0
        AND SUM(S.INTRANSITQTY) = 0
        AND SUM(S.ONPOQTY) = 0
        AND SUM(S.SOOTHER) = 0
        AND SUM(S.POOTHER) = 0
        AND SUM(S.TOTALDEMAND) = 0
        AND SUM(S.SUPPLYOP) = 0
        AND SUM(COALESCE(D.TOTAL_DEMAND, 0)) = 0
        AND MAX(COALESCE(HS.H_ADU, 0)) = 0
    )
),

WITH_ADU AS (
    SELECT
        *,
        ROUND(CASE WHEN SUM(H_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER) = 0
                THEN 1.0 / COUNT(*) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER)
           ELSE H_ADU / NULLIF(SUM(H_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER),0)
      END, 2) AS PERCENTAGE,

        ROUND(FCST_TOTAL * (CASE WHEN SUM(H_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER) = 0
                THEN 1.0 / COUNT(*) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER)
           ELSE H_ADU / NULLIF(SUM(H_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER),0)
      END), 2) AS F_ADU
    FROM FINAL
),

WITH_DC AS (
    SELECT
        *,
        COALESCE(ROUND((ONHANDQTY - EXPIREDQTY - BLOCKEDQTY - N30D_SO) / NULLIF(F_ADU, 0), 2), 0) AS SOH_DC,
        COALESCE(ROUND(INTRANSITQTY / NULLIF(F_ADU, 0), 2), 0) AS INTRANSIT_DC,
        COALESCE(ROUND(ONPOQTY / NULLIF(F_ADU, 0), 2), 0) AS ONPO_DC,
        COALESCE(ROUND(SUM(ONHANDQTY - EXPIREDQTY - BLOCKEDQTY - N30D_SO) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER)
            / NULLIF(SUM(F_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER), 0), 2), 0) AS NAT_SOH_DC,
        COALESCE(ROUND(SUM(INTRANSITQTY) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER)
            / NULLIF(SUM(F_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER), 0), 2), 0) AS NAT_INTRANSIT_DC,
        COALESCE(ROUND(SUM(ONPOQTY) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER)
            / NULLIF(SUM(F_ADU) OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER), 0), 2), 0) AS NAT_ONPO_DC,
        ONHANDQTY - EXPIREDQTY - BLOCKEDQTY - Q_SO - Q_SB + INTRANSITQTY + ONPOQTY AS ROW_NET_POSITION,
        SUM(ONHANDQTY - EXPIREDQTY - BLOCKEDQTY - Q_SO - Q_SB + INTRANSITQTY + ONPOQTY)
            OVER (PARTITION BY COMPANY_COUNTRY, PRODUCT_FULL_SEGMENTS_NUMBER) AS NAT_NET_POSITION
    FROM WITH_ADU
),

WITH_ORDER AS (
    SELECT
        *,
        ROUND(NAT_SOH_DC + NAT_INTRANSIT_DC + NAT_ONPO_DC, 2) AS NAT_PIPELINE_DC,
        COALESCE(ROUND(SOH_DC + INTRANSIT_DC + ONPO_DC, 2), 0) AS ROW_PIPELINE_DC,
        COALESCE(ROUND(CASE
            WHEN INVENTORYTYPE REGEXP '.*[ABMR].*' THEN
                CASE
                    WHEN (NAT_SOH_DC + NAT_INTRANSIT_DC + NAT_ONPO_DC) > IDEAL_PIPELINE + 30 THEN 0
                    WHEN (SOH_DC + INTRANSIT_DC + ONPO_DC) <= IDEAL_PIPELINE - 30 THEN
                        LEAST(GREATEST(
                            (IDEAL_PIPELINE - (SOH_DC + INTRANSIT_DC + ONPO_DC)) * F_ADU,
                            MIN_DAYS * F_ADU
                        ), MAX_DAYS * F_ADU)
                    ELSE 0
                END
            ELSE
                CASE
                    WHEN NAT_NET_POSITION > 0 THEN 0
                    ELSE
                        CASE
                            WHEN NAT_NET_POSITION < 0 AND ROW_NET_POSITION < 0
                            THEN
                             GREATEST(-NAT_NET_POSITION, 0)
                        END
                END
        END, 2), 0) AS RAW_ORDER_QTY
    FROM WITH_DC
),

WITH_FINAL AS (
    SELECT
        *,
        COALESCE(ROUND(CASE
            WHEN RAW_ORDER_QTY = 0 THEN 0
            ELSE
                CASE
                    WHEN ITEM_NET_WEIGHT > 0 THEN
                        CEIL(GREATEST(RAW_ORDER_QTY, COALESCE(NULLIF(MOQ, 0), RAW_ORDER_QTY)) / ITEM_NET_WEIGHT) * ITEM_NET_WEIGHT
                    ELSE
                        GREATEST(RAW_ORDER_QTY, COALESCE(NULLIF(MOQ, 0), RAW_ORDER_QTY))
                END
        END, 2), 0) AS ORDER_QTY
    FROM WITH_ORDER
)

SELECT
    *,
    ROUND(ORDER_QTY * UNITCOST, 2) AS ORDER_AMOUNT,
    CASE
        WHEN STOCK_STATUS IN ('Overstock', 'High Stock', 'No FCST - Overstock')
        THEN ROUND(NAT_NET_POSITION * UNITCOST, 2)
        ELSE 0
    END AS OVERSTOCK_VALUE
FROM (
    SELECT
        *,
        COALESCE(ROUND(ORDER_QTY / NULLIF(F_ADU, 0), 2), 0) AS ORDER_DC,
        CASE
            WHEN FCST_TOTAL = 0 AND NAT_NET_POSITION > 0 THEN 'No FCST - Overstock'
            WHEN INVENTORYTYPE REGEXP '.*[ABMR].*' THEN
                CASE
                    WHEN NAT_PIPELINE_DC < IDEAL_PIPELINE - 60 THEN 'Stockout Risk'
                    WHEN NAT_PIPELINE_DC < IDEAL_PIPELINE - 30 THEN 'Low Stock'
                    WHEN NAT_PIPELINE_DC <= IDEAL_PIPELINE + 30 THEN 'Healthy'
                    WHEN NAT_PIPELINE_DC <= IDEAL_PIPELINE + 60 THEN 'High Stock'
                    ELSE 'Overstock'
                END
            ELSE
                CASE
                    WHEN NAT_NET_POSITION < 0 THEN 'Low Stock'
                    WHEN NAT_NET_POSITION = 0 THEN 'Healthy'
                    ELSE 'Overstock'
                END
        END AS STOCK_STATUS
    FROM WITH_FINAL
)
ORDER BY FCST_TOTAL DESC, F_ADU DESC
"""


def _seconds_until_8am():
    """Calculate seconds until the next 8:00 AM (local time)."""
    now = datetime.now()
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return int((target - now).total_seconds())


@st.cache_data(ttl=_seconds_until_8am())
def load_drp_data():
    conn = get_connection()
    df = conn.query(DRP_SQL)
    import decimal
    for col in df.columns:
        series = df[col]
        if hasattr(series, 'dtype') and series.dtype == object and len(df) > 0:
            sample = series.dropna().iloc[0] if not series.dropna().empty else None
            if isinstance(sample, decimal.Decimal):
                df[col] = series.apply(lambda x: float(x) if x is not None else None)
    return df


def format_number(val, decimals=0):
    if pd.isna(val) or val is None:
        return ""
    if decimals == 0:
        return f"{int(val):,}"
    return f"{val:,.{decimals}f}"


# CSS colors for stock status styling in dataframes
STATUS_BG_COLORS = {
    "Stockout Risk": "background-color: #FEE2E2; color: #991B1B",
    "Low Stock": "background-color: #FFEDD5; color: #9A3412",
    "Healthy": "background-color: #DCFCE7; color: #166534",
    "High Stock": "background-color: #DBEAFE; color: #1E40AF",
    "Overstock": "background-color: #EDE9FE; color: #5B21B6",
    "No FCST - Overstock": "background-color: #F3E8FF; color: #6B21A8",
}


def style_stock_status(val):
    return STATUS_BG_COLORS.get(val, "")


def main():
    # Reduce default top padding to move content up
    st.markdown("""
        <style>
            .block-container { padding-top: 1rem; }
        </style>
    """, unsafe_allow_html=True)

    st.title("DRP System")
    st.caption("Distribution Replenishment Planning - IMCD ANZ")

    with st.spinner("Loading DRP data from Snowflake..."):
        try:
            df = load_drp_data()
        except Exception as e:
            st.error(f"Failed to load data: {e}")
            return

    if df.empty:
        st.warning("No data returned from DRP query.")
        return

    # --- Sidebar Filters ---
    with st.sidebar:
        st.header("Filters")

        countries = sorted(df["COMPANY_COUNTRY"].dropna().unique().tolist())
        sel_country = st.multiselect("Country", countries, default=countries)

        filtered = df[df["COMPANY_COUNTRY"].isin(sel_country)] if sel_country else df

        states = sorted(filtered["STATE"].dropna().unique().tolist())
        sel_state = st.multiselect("State", states)

        vendors = sorted(filtered["VENDOR_NAME"].dropna().unique().tolist())
        sel_vendor = st.multiselect("Vendor", vendors)

        principals = sorted(filtered["PRODUCT_PRINCIPAL_NAME"].dropna().unique().tolist())
        sel_principal = st.multiselect("Principal", principals)

        buyers = sorted(filtered["BUYER_NAME"].dropna().unique().tolist())
        sel_buyer = st.multiselect("Buyer", buyers)

        pm_names = sorted(filtered["LOCAL_PRODUCTMANAGER_NAME"].dropna().unique().tolist())
        sel_pm = st.multiselect("Local PM Name", pm_names)

        inv_types = sorted(filtered["INVENTORYTYPE"].dropna().unique().tolist())
        sel_inv_type = st.multiselect("Inventory Type", inv_types)

        product_search = st.text_input("Product Search", placeholder="Search by name or number...")

        stock_statuses = sorted(df["STOCK_STATUS"].dropna().unique().tolist())
        sel_status = st.multiselect("Stock Status", stock_statuses)

        st.divider()
        only_orders = st.checkbox("Show only items with suggested orders", value=False)

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if sel_country:
        mask &= df["COMPANY_COUNTRY"].isin(sel_country)
    if sel_state:
        mask &= df["STATE"].isin(sel_state)
    if sel_vendor:
        mask &= df["VENDOR_NAME"].isin(sel_vendor)
    if sel_principal:
        mask &= df["PRODUCT_PRINCIPAL_NAME"].isin(sel_principal)
    if sel_buyer:
        mask &= df["BUYER_NAME"].isin(sel_buyer)
    if sel_pm:
        mask &= df["LOCAL_PRODUCTMANAGER_NAME"].isin(sel_pm)
    if sel_inv_type:
        mask &= df["INVENTORYTYPE"].isin(sel_inv_type)
    if product_search:
        search_lower = product_search.lower()
        mask &= (
            df["PRODUCT_NAME"].str.lower().str.contains(search_lower, na=False)
            | df["PRODUCT_FULL_SEGMENTS_NUMBER"].str.lower().str.contains(search_lower, na=False)
        )
    if sel_status:
        mask &= df["STOCK_STATUS"].isin(sel_status)
    if only_orders:
        mask &= df["ORDER_QTY"] > 0

    filtered_df = df[mask].copy()

    # --- KPI Summary ---
    total_items = len(filtered_df)
    items_needing_order = (filtered_df["ORDER_QTY"] > 0).sum()
    total_order_value = (filtered_df["ORDER_QTY"] * filtered_df["UNITCOST"]).sum()
    avg_pipeline_dc = filtered_df["NAT_PIPELINE_DC"].mean()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total SKU-State Lines", format_number(total_items))
    col2.metric("Items Needing Order", format_number(items_needing_order))
    col3.metric("Total Order Value", f"${format_number(total_order_value, 0)}")
    col4.metric("Avg National Pipeline DC", format_number(avg_pipeline_dc, 1) if not pd.isna(avg_pipeline_dc) else "N/A")

    st.divider()

    # --- Tabs ---
    tab_overview, tab_orders, tab_stock, tab_detail = st.tabs(["Overview", "Order Suggestions", "Stock Level Analysis", "Product Detail"])

    with tab_overview:
        st.subheader("Inventory Overview")
        display_cols = [
            "PRODUCT_FULL_SEGMENTS_NUMBER", "PRODUCT_NAME", "PRODUCT_PACKAGE_DESCRIPTION",
            "COMPANY_COUNTRY", "STATE",
            "INVENTORYTYPE", "VENDOR_NAME", "STOCK_STATUS",
            "ONHANDQTY", "EXPIREDQTY", "BLOCKEDQTY", "INTRANSITQTY", "ONPOQTY",
            "F_ADU", "SOH_DC", "INTRANSIT_DC", "ONPO_DC", "NAT_PIPELINE_DC", "IDEAL_PIPELINE",
            "ORDER_QTY", "ORDER_AMOUNT", "OVERSTOCK_VALUE", "UNITCOST",
        ]
        display_df = filtered_df[display_cols].copy()
        display_df.columns = [
            "Product #", "Product Name", "Package",
            "Country", "State",
            "Inv Type", "Vendor", "Stock Status",
            "On Hand", "Expired", "Blocked", "In Transit", "On PO",
            "Fcst ADU", "SOH DC", "Intransit DC", "On PO DC", "Nat Pipeline DC", "Ideal Pipeline",
            "Suggested Order", "Order Amount", "Overstock Value", "Unit Cost",
        ]

        styled_overview = display_df.style.map(style_stock_status, subset=["Stock Status"])
        st.dataframe(
            styled_overview,
            use_container_width=True,
            hide_index=True,
            height=800,
            column_config={
                "On Hand": st.column_config.NumberColumn(format="%d"),
                "Expired": st.column_config.NumberColumn(format="%d"),
                "Blocked": st.column_config.NumberColumn(format="%d"),
                "In Transit": st.column_config.NumberColumn(format="%d"),
                "On PO": st.column_config.NumberColumn(format="%d"),
                "Fcst ADU": st.column_config.NumberColumn(format="%.2f"),
                "SOH DC": st.column_config.NumberColumn(format="%.1f"),
                "Intransit DC": st.column_config.NumberColumn(format="%.1f"),
                "On PO DC": st.column_config.NumberColumn(format="%.1f"),
                "Nat Pipeline DC": st.column_config.NumberColumn(format="%.1f"),
                "Ideal Pipeline": st.column_config.NumberColumn(format="%.0f"),
                "Suggested Order": st.column_config.NumberColumn(format="%.0f"),
                "Order Amount": st.column_config.NumberColumn(format="$%,.0f"),
                "Overstock Value": st.column_config.NumberColumn(format="$%,.0f"),
                "Unit Cost": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        st.caption(f"Showing {len(display_df):,} rows")

    with tab_orders:
        st.subheader("Order Suggestions")

        order_df = filtered_df[filtered_df["ORDER_QTY"] > 0].copy()

        if order_df.empty:
            st.info("No order suggestions for the current filter selection.")
        else:
            # Summary by country
            country_summary = (
                order_df.groupby("COMPANY_COUNTRY")
                .agg(
                    Items=("ORDER_QTY", "count"),
                    Total_Qty=("ORDER_QTY", "sum"),
                    Total_Value=("ORDER_QTY", lambda x: (x * order_df.loc[x.index, "UNITCOST"]).sum()),
                )
                .reset_index()
            )
            country_summary.columns = ["Country", "# Items", "Total Order Qty", "Total Order Value"]

            st.dataframe(
                country_summary,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Total Order Qty": st.column_config.NumberColumn(format="%,.0f"),
                    "Total Order Value": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

            st.divider()

            # Order details with editable quantity
            st.markdown("**Review and adjust order quantities below:**")

            order_display = order_df[[
                "PRODUCT_FULL_SEGMENTS_NUMBER", "PRODUCT_NAME", "COMPANY_COUNTRY", "STATE",
                "VENDOR_NAME", "PRODUCT_PRINCIPAL_NAME",
                "F_ADU", "NAT_PIPELINE_DC", "IDEAL_PIPELINE",
                "RAW_ORDER_QTY", "ORDER_QTY", "MOQ", "UNITCOST",
                "LEADTIMELEVEL", "ITEM_NET_WEIGHT",
            ]].copy()

            order_display["ORDER_VALUE"] = order_display["ORDER_QTY"] * order_display["UNITCOST"]

            order_display.columns = [
                "Product #", "Product Name", "Country", "State",
                "Vendor", "Principal",
                "Fcst ADU", "Nat Pipeline DC", "Ideal Pipeline",
                "Raw Order Qty", "Suggested Order", "MOQ", "Unit Cost",
                "Lead Time (days)", "Net Weight",
                "Order Value",
            ]

            edited_orders = st.data_editor(
                order_display,
                use_container_width=True,
                hide_index=True,
                height=500,
                disabled=[
                    "Product #", "Product Name", "Country", "State",
                    "Vendor", "Principal",
                    "Fcst ADU", "Nat Pipeline DC", "Ideal Pipeline",
                    "Raw Order Qty", "MOQ", "Unit Cost",
                    "Lead Time (days)", "Net Weight", "Order Value",
                ],
                column_config={
                    "Fcst ADU": st.column_config.NumberColumn(format="%.2f"),
                    "Nat Pipeline DC": st.column_config.NumberColumn(format="%.1f"),
                    "Ideal Pipeline": st.column_config.NumberColumn(format="%.0f"),
                    "Raw Order Qty": st.column_config.NumberColumn(format="%.0f"),
                    "Suggested Order": st.column_config.NumberColumn(format="%.0f"),
                    "MOQ": st.column_config.NumberColumn(format="%.0f"),
                    "Unit Cost": st.column_config.NumberColumn(format="$%.2f"),
                    "Lead Time (days)": st.column_config.NumberColumn(format="%d"),
                    "Net Weight": st.column_config.NumberColumn(format="%.2f"),
                    "Order Value": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

            st.caption(f"{len(order_display):,} items with suggested orders")

            col_a, col_b = st.columns([1, 4])
            with col_a:
                if st.button("Export Orders to CSV", type="primary"):
                    csv = edited_orders.to_csv(index=False)
                    st.download_button(
                        label="Download CSV",
                        data=csv,
                        file_name="drp_orders.csv",
                        mime="text/csv",
                    )

    with tab_stock:
        st.subheader("Stock Level Analysis")

        adf = filtered_df.copy()

        # Ensure ORDER_AMOUNT and OVERSTOCK_VALUE exist (compute if missing from SQL)
        if "ORDER_AMOUNT" not in adf.columns:
            adf["ORDER_AMOUNT"] = adf["ORDER_QTY"] * adf["UNITCOST"]
        if "OVERSTOCK_VALUE" not in adf.columns:
            adf["OVERSTOCK_VALUE"] = adf.apply(
                lambda r: r["NAT_NET_POSITION"] * r["UNITCOST"]
                if r["STOCK_STATUS"] in ("Overstock", "High Stock", "No FCST - Overstock") else 0,
                axis=1,
            )

        # --- KPI: Distinct product counts by stock status ---
        # Get one status per product (use the "worst" status across states)
        product_status_df = adf.drop_duplicates(subset=["PRODUCT_FULL_SEGMENTS_NUMBER", "STOCK_STATUS"])
        total_products = adf["PRODUCT_FULL_SEGMENTS_NUMBER"].nunique()
        status_product_counts = product_status_df.groupby("STOCK_STATUS")["PRODUCT_FULL_SEGMENTS_NUMBER"].nunique()
        all_statuses = sorted(status_product_counts.index.tolist())

        kpi_cols = st.columns(min(len(all_statuses) + 1, 8))
        kpi_cols[0].metric("Total Products", f"{total_products:,}")
        for i, status in enumerate(all_statuses):
            if i + 1 < len(kpi_cols):
                kpi_cols[i + 1].metric(status, f"{status_product_counts.get(status, 0):,}")

        st.divider()

        # --- Helper: pivot distinct product counts by stock status per group ---
        def build_status_pivot(group_cols, col_labels):
            deduped = adf.groupby(group_cols + ["PRODUCT_FULL_SEGMENTS_NUMBER"]).agg(
                STOCK_STATUS=("STOCK_STATUS", "first"),
                ORDER_AMOUNT=("ORDER_AMOUNT", "sum"),
                OVERSTOCK_VALUE=("OVERSTOCK_VALUE", "sum"),
            ).reset_index()
            deduped = fix_overstock(deduped)

            product_counts = deduped.groupby(group_cols)["PRODUCT_FULL_SEGMENTS_NUMBER"].nunique().reset_index()
            product_counts.columns = group_cols + ["Products"]

            status_pivot = deduped.groupby(group_cols + ["STOCK_STATUS"])["PRODUCT_FULL_SEGMENTS_NUMBER"].nunique().unstack(fill_value=0).reset_index()

            value_agg = deduped.groupby(group_cols).agg(
                Order_Amount=("ORDER_AMOUNT", "sum"),
                Overstock_Value=("OVERSTOCK_VALUE", "sum"),
            ).reset_index()

            result = product_counts.merge(status_pivot, on=group_cols, how="left").merge(value_agg, on=group_cols, how="left")

            rename_map = {old: new for old, new in zip(group_cols, col_labels)}
            result = result.rename(columns=rename_map)
            result = result.rename(columns={
                "Order_Amount": "Order Amount",
                "Overstock_Value": "Overstock Value",
            })

            return result.sort_values("Order Amount", ascending=False)

        def get_column_config(df_display):
            cfg = {
                "Products": st.column_config.NumberColumn(format="%d"),
                "Order Amount": st.column_config.NumberColumn(format="$%,.0f"),
                "Overstock Value": st.column_config.NumberColumn(format="$%,.0f"),
            }
            for col in df_display.columns:
                if col in all_statuses:
                    cfg[col] = st.column_config.NumberColumn(format="%d")
            return cfg

        # --- By Buyer: expanders with summary in label, nested stock status > inv type ---
        st.markdown("#### By Buyer")

        # Color mapping for stock statuses (Streamlit named colors for expanders)
        STATUS_COLORS = {
            "Stockout Risk": "red",
            "Low Stock": "orange",
            "Healthy": "green",
            "High Stock": "blue",
            "Overstock": "violet",
            "No FCST - Overstock": "violet",
        }



        def fmt_dollar(val):
            if pd.isna(val) or val is None:
                return "$0"
            return f"${val:,.0f}"

        OVERSTOCK_STATUSES = {"Overstock", "High Stock", "No FCST - Overstock"}

        def fix_overstock(deduped_df):
            """Ensure ORDER_AMOUNT and OVERSTOCK_VALUE are mutually exclusive by status."""
            overstock_mask = deduped_df["STOCK_STATUS"].isin(OVERSTOCK_STATUSES)
            deduped_df.loc[~overstock_mask, "OVERSTOCK_VALUE"] = 0
            deduped_df.loc[overstock_mask, "ORDER_AMOUNT"] = 0
            return deduped_df

        # Pre-compute buyer-level summary
        buyer_deduped = adf.groupby(["BUYER_NAME", "PRODUCT_FULL_SEGMENTS_NUMBER"]).agg(
            STOCK_STATUS=("STOCK_STATUS", "first"),
            ORDER_AMOUNT=("ORDER_AMOUNT", "sum"),
            OVERSTOCK_VALUE=("OVERSTOCK_VALUE", "sum"),
        ).reset_index()
        buyer_deduped = fix_overstock(buyer_deduped)

        buyer_summary = buyer_deduped.groupby("BUYER_NAME").agg(
            Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
            Order_Amount=("ORDER_AMOUNT", "sum"),
            Overstock_Value=("OVERSTOCK_VALUE", "sum"),
        ).reset_index().sort_values("Order_Amount", ascending=False)

        val_cfg = {
            "Products": st.column_config.NumberColumn(format="%d"),
            "Order Amount": st.column_config.NumberColumn(format="$%,.0f"),
            "Overstock Value": st.column_config.NumberColumn(format="$%,.0f"),
        }

        for _, brow in buyer_summary.iterrows():
            buyer = brow["BUYER_NAME"]
            products_count = int(brow["Products"])
            order_amt = fmt_dollar(brow["Order_Amount"])
            overstock_val = fmt_dollar(brow["Overstock_Value"])
            label = f"{buyer}  |  Products: {products_count:,}  |  :blue[Order Amount: **{order_amt}**]  |  :orange[Overstock Value: **{overstock_val}**]"

            with st.expander(label):
                buyer_data = adf[adf["BUYER_NAME"] == buyer]

                # Deduplicate: one row per product per inv type
                bi_deduped = buyer_data.groupby(["INVENTORYTYPE", "PRODUCT_FULL_SEGMENTS_NUMBER"]).agg(
                    STOCK_STATUS=("STOCK_STATUS", "first"),
                    ORDER_AMOUNT=("ORDER_AMOUNT", "sum"),
                    OVERSTOCK_VALUE=("OVERSTOCK_VALUE", "sum"),
                ).reset_index()
                bi_deduped = fix_overstock(bi_deduped)

                inv_summary = bi_deduped.groupby("INVENTORYTYPE").agg(
                    Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
                    Order_Amount=("ORDER_AMOUNT", "sum"),
                    Overstock_Value=("OVERSTOCK_VALUE", "sum"),
                ).reset_index().sort_values("Order_Amount", ascending=False)

                for _, irow in inv_summary.iterrows():
                    inv_type = irow["INVENTORYTYPE"]
                    i_products = int(irow["Products"])
                    i_order = fmt_dollar(irow["Order_Amount"])
                    i_overstock = fmt_dollar(irow["Overstock_Value"])
                    i_label = f"**{inv_type}**  |  Products: {i_products:,}  |  :blue[Order Amount: **{i_order}**]  |  :orange[Overstock Value: **{i_overstock}**]"

                    with st.expander(i_label):
                        # Stock status breakdown within this buyer + inv type
                        inv_data = bi_deduped[bi_deduped["INVENTORYTYPE"] == inv_type]
                        status_tbl = inv_data.groupby("STOCK_STATUS").agg(
                            Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
                            **{"Order Amount": ("ORDER_AMOUNT", "sum")},
                            **{"Overstock Value": ("OVERSTOCK_VALUE", "sum")},
                        ).reset_index().rename(columns={"STOCK_STATUS": "Stock Status"})
                        status_tbl = status_tbl.sort_values("Order Amount", ascending=False)
                        styled_tbl = status_tbl.style.map(style_stock_status, subset=["Stock Status"])
                        st.dataframe(styled_tbl, use_container_width=True, hide_index=True, column_config=val_cfg)

        st.divider()

        st.markdown("#### By Local Product Manager")

        pm_deduped = adf.groupby(["LOCAL_PRODUCTMANAGER_NAME", "PRODUCT_FULL_SEGMENTS_NUMBER"]).agg(
            STOCK_STATUS=("STOCK_STATUS", "first"),
            ORDER_AMOUNT=("ORDER_AMOUNT", "sum"),
            OVERSTOCK_VALUE=("OVERSTOCK_VALUE", "sum"),
        ).reset_index()
        pm_deduped = fix_overstock(pm_deduped)

        pm_summary = pm_deduped.groupby("LOCAL_PRODUCTMANAGER_NAME").agg(
            Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
            Order_Amount=("ORDER_AMOUNT", "sum"),
            Overstock_Value=("OVERSTOCK_VALUE", "sum"),
        ).reset_index().sort_values("Order_Amount", ascending=False)

        for _, pmrow in pm_summary.iterrows():
            pm_name = pmrow["LOCAL_PRODUCTMANAGER_NAME"]
            pm_products = int(pmrow["Products"])
            pm_order = fmt_dollar(pmrow["Order_Amount"])
            pm_overstock = fmt_dollar(pmrow["Overstock_Value"])
            pm_label = f"{pm_name}  |  Products: {pm_products:,}  |  :blue[Order Amount: **{pm_order}**]  |  :orange[Overstock Value: **{pm_overstock}**]"

            with st.expander(pm_label):
                pm_data = adf[adf["LOCAL_PRODUCTMANAGER_NAME"] == pm_name]

                pi_deduped = pm_data.groupby(["INVENTORYTYPE", "PRODUCT_FULL_SEGMENTS_NUMBER"]).agg(
                    STOCK_STATUS=("STOCK_STATUS", "first"),
                    ORDER_AMOUNT=("ORDER_AMOUNT", "sum"),
                    OVERSTOCK_VALUE=("OVERSTOCK_VALUE", "sum"),
                ).reset_index()
                pi_deduped = fix_overstock(pi_deduped)

                pi_summary = pi_deduped.groupby("INVENTORYTYPE").agg(
                    Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
                    Order_Amount=("ORDER_AMOUNT", "sum"),
                    Overstock_Value=("OVERSTOCK_VALUE", "sum"),
                ).reset_index().sort_values("Order_Amount", ascending=False)

                for _, pirow in pi_summary.iterrows():
                    pi_type = pirow["INVENTORYTYPE"]
                    pi_products = int(pirow["Products"])
                    pi_order = fmt_dollar(pirow["Order_Amount"])
                    pi_overstock = fmt_dollar(pirow["Overstock_Value"])
                    pi_label = f"**{pi_type}**  |  Products: {pi_products:,}  |  :blue[Order Amount: **{pi_order}**]  |  :orange[Overstock Value: **{pi_overstock}**]"

                    with st.expander(pi_label):
                        pi_data = pi_deduped[pi_deduped["INVENTORYTYPE"] == pi_type]
                        pi_status_tbl = pi_data.groupby("STOCK_STATUS").agg(
                            Products=("PRODUCT_FULL_SEGMENTS_NUMBER", "nunique"),
                            **{"Order Amount": ("ORDER_AMOUNT", "sum")},
                            **{"Overstock Value": ("OVERSTOCK_VALUE", "sum")},
                        ).reset_index().rename(columns={"STOCK_STATUS": "Stock Status"})
                        pi_status_tbl = pi_status_tbl.sort_values("Order Amount", ascending=False)
                        styled_pi = pi_status_tbl.style.map(style_stock_status, subset=["Stock Status"])
                        st.dataframe(styled_pi, use_container_width=True, hide_index=True, column_config=val_cfg)

    with tab_detail:
        st.subheader("Product Detail Lookup")
        products = sorted(filtered_df["PRODUCT_FULL_SEGMENTS_NUMBER"].unique().tolist())
        sel_product = st.selectbox("Select Product", products, index=None, placeholder="Choose a product...")

        if sel_product:
            prod_rows = filtered_df[filtered_df["PRODUCT_FULL_SEGMENTS_NUMBER"] == sel_product]

            if not prod_rows.empty:
                row = prod_rows.iloc[0]
                st.markdown(f"### {row['PRODUCT_NAME']}")
                st.caption(f"{sel_product} | {row.get('PRODUCT_PACKAGE_DESCRIPTION', '')}")

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Country", row["COMPANY_COUNTRY"])
                c2.metric("Inventory Type", row["INVENTORYTYPE"])
                c3.metric("Vendor", str(row["VENDOR_NAME"])[:30])
                c4.metric("Unit Cost", f"${row['UNITCOST']:.2f}" if pd.notna(row["UNITCOST"]) else "N/A")

                st.divider()

                # National aggregation across all states for this product+country
                nat_rows = filtered_df[
                    (filtered_df["PRODUCT_FULL_SEGMENTS_NUMBER"] == sel_product)
                    & (filtered_df["COMPANY_COUNTRY"] == row["COMPANY_COUNTRY"])
                ]
                nat_onhand = nat_rows["ONHANDQTY"].sum()
                nat_expired = nat_rows["EXPIREDQTY"].sum()
                nat_blocked = nat_rows["BLOCKEDQTY"].sum()
                nat_intransit = nat_rows["INTRANSITQTY"].sum()
                nat_onpo = nat_rows["ONPOQTY"].sum()
                nat_fadu = nat_rows["F_ADU"].sum()
                nat_n30d_so = nat_rows["N30D_SO"].sum()
                nat_soh = nat_onhand - nat_expired - nat_blocked - nat_n30d_so
                nat_q_so = nat_rows["Q_SO"].sum()
                nat_q_sb = nat_rows["Q_SB"].sum()
                nat_total_demand = nat_rows["TOTAL_DEMAND"].sum()
                nat_order_qty = nat_rows["ORDER_QTY"].sum()
                nat_raw_order_qty = nat_rows["RAW_ORDER_QTY"].sum()
                nat_stock_value = nat_onhand * row["UNITCOST"] if pd.notna(row["UNITCOST"]) else 0
                nat_soh_dc = round(nat_soh / nat_fadu, 1) if nat_fadu else None
                nat_intransit_dc = round(nat_intransit / nat_fadu, 1) if nat_fadu else None
                nat_onpo_dc = round(nat_onpo / nat_fadu, 1) if nat_fadu else None
                nat_pipeline_dc = round((nat_soh + nat_intransit + nat_onpo) / nat_fadu, 1) if nat_fadu else None

                st.markdown(f"**National Stock Position ({row['COMPANY_COUNTRY']})**")
                n1, n2, n3, n4, n5 = st.columns(5)
                n1.metric("Nat On Hand", format_number(nat_onhand))
                n2.metric("Nat Expired", format_number(nat_expired))
                n3.metric("Nat Blocked", format_number(nat_blocked))
                n4.metric("Nat In Transit", format_number(nat_intransit))
                n5.metric("Nat On PO", format_number(nat_onpo))

                st.markdown(f"**National Demand & Supply ({row['COMPANY_COUNTRY']})**")
                nd1, nd2, nd3, nd4, nd5 = st.columns(5)
                nd1.metric("Nat Fcst ADU", format_number(nat_fadu, 2))
                nd2.metric("Nat 30D SO", format_number(nat_n30d_so))
                nd3.metric("Nat Total SO", format_number(nat_q_so))
                nd4.metric("Nat Total SB", format_number(nat_q_sb))
                nd5.metric("Nat Total Demand", format_number(nat_total_demand))

                st.markdown(f"**National Days Cover ({row['COMPANY_COUNTRY']})**")
                ndc1, ndc2, ndc3, ndc4, ndc5 = st.columns(5)
                ndc1.metric("Nat SOH DC", format_number(nat_soh_dc, 1) if nat_soh_dc is not None else "N/A")
                ndc2.metric("Nat Intransit DC", format_number(nat_intransit_dc, 1) if nat_intransit_dc is not None else "N/A")
                ndc3.metric("Nat On PO DC", format_number(nat_onpo_dc, 1) if nat_onpo_dc is not None else "N/A")
                ndc4.metric("Nat Pipeline DC", format_number(nat_pipeline_dc, 1) if nat_pipeline_dc is not None else "N/A")
                ndc5.metric("Ideal Pipeline", format_number(row["IDEAL_PIPELINE"], 0))

                st.markdown(f"**National Order Suggestion ({row['COMPANY_COUNTRY']})**")
                no1, no2, no3, no4, no5 = st.columns(5)
                no1.metric("Nat Raw Order Qty", format_number(nat_raw_order_qty, 0))
                no2.metric("Nat Order Qty", format_number(nat_order_qty, 0))
                no3.metric("Nat Stock Value", f"${format_number(nat_stock_value, 0)}")
                no4.metric("MOQ", format_number(row["MOQ"], 0))
                no5.metric("Lead Time", f"{format_number(row['LEADTIMELEVEL'])} days")

                # Show all states for this product
                if len(prod_rows) > 1:
                    st.divider()
                    st.markdown("**All States for this Product**")
                    state_cols = [
                        "STATE", "ONHANDQTY", "INTRANSITQTY", "ONPOQTY",
                        "F_ADU", "SOH_DC", "NAT_PIPELINE_DC", "ORDER_QTY",
                    ]
                    state_df = prod_rows[state_cols].copy()
                    state_df.columns = [
                        "State", "On Hand", "In Transit", "On PO",
                        "Fcst ADU", "SOH DC", "Nat Pipeline DC", "Suggested Order",
                    ]
                    st.dataframe(state_df, use_container_width=True, hide_index=True)


def check_password():
    if st.session_state.get("authenticated"):
        return True
    st.markdown("<h2 style='text-align:center; margin-top:15vh;'>DRP System - IMCD ANZ</h2>", unsafe_allow_html=True)
    col_l, col_m, col_r = st.columns([1, 1, 1])
    with col_m:
        pwd = st.text_input("Enter password to access the system", type="password", key="pwd_input")
        if st.button("Login", type="primary", use_container_width=True):
            if pwd == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


if __name__ == "__main__":
    if check_password():
        main()
