# MZQA API — FastAPI backend

REST backend for the MZQA Terminal V2 (Next.js frontend in `../web`).

## Setup

```powershell
# from project root (MZQA/)
"C:\Bastian\anaconda3\python.exe" -m pip install -r api\requirements.txt
copy api\.env.example api\.env
```

Edit `api/.env` with your `DATABASE_URL` (defaults to the local Postgres).

## Run

```powershell
# from project root
"C:\Bastian\anaconda3\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Then:

- Swagger UI: http://127.0.0.1:8000/docs
- Health:     http://127.0.0.1:8000/api/healthz

## Endpoints (smoke-tested 2026-05-19)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/healthz` | `{status, db, schema}` |
| GET | `/api/meta/filters?jurisdiction=US` | Exchange / sector / industry options |
| GET | `/api/companies?jurisdiction=US&exchange=Nasdaq&limit=200` | Ticker universe (filterable) |
| GET | `/api/entity/{ticker}?jurisdiction=US` | Identity panel data |
| GET | `/api/kpis/{ticker}?jurisdiction=US&period=FY` | 6 KPI chips |
| GET | `/api/filing-coverage/{ticker}?jurisdiction=US` | Dot matrix per FY/Q1/Q2/Q3 |
| GET | `/api/statement/{ticker}?jurisdiction=US&statement=BS&year_min=2021&year_max=2025` | BS/IS/CF rows with values + CAGR |
| GET | `/api/analytics/{ticker}?jurisdiction=US` | Metrics table grouped by category |

## Schema notes

- All queries assume `search_path = sec, public` (set via `asyncpg` `server_settings`)
- `dim_company_us`, `dim_company_jp` provide the entity universe
- `fact_fundamentals_std_us` / `_jp` provide line-item values
- `fact_metrics_us` / `_jp` provide computed FY metrics
- `fact_market_metrics` provides daily market-data metrics (market_capitalization, stock_price)
- `ref_standardized_line_items` provides labels + display ordering (`display_order_us_gaap` / `_jp_gaap`)
