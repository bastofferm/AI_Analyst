-- Correct metric display semantics for working-capital day metrics and
-- jurisdiction-specific regional metrics.

SET search_path TO sec;

UPDATE ref_metric_definitions
SET unit_type = 'DAYS'
WHERE metric_id IN (
    'days_sales_outstanding',
    'days_inventory_outstanding',
    'days_payable_outstanding',
    'days_sales_outstanding_trend',
    'cash_conversion_cycle'
);

UPDATE ref_metric_definitions
SET sector_scope = 'jp_gaap',
    unit_type = 'CCY'
WHERE metric_id = 'goodwill_amortisation_addback';

UPDATE ref_metric_definitions
SET sector_scope = 'jp_gaap',
    unit_type = 'DEC'
WHERE metric_id IN (
    'ordinary_income_japan_gaap_metric',
    'special_gains_losses_japan_gaap_metric'
);

UPDATE ref_metric_definitions
SET sector_scope = 'ifrs',
    unit_type = 'CCY'
WHERE metric_id = 'ifrs16_lease_interest_split';

UPDATE fact_metrics_us
SET unit_type = 'DAYS'
WHERE metric_id IN (
    'days_sales_outstanding',
    'days_inventory_outstanding',
    'days_payable_outstanding',
    'days_sales_outstanding_trend',
    'cash_conversion_cycle'
);

UPDATE fact_metrics_jp
SET unit_type = 'DAYS'
WHERE metric_id IN (
    'days_sales_outstanding',
    'days_inventory_outstanding',
    'days_payable_outstanding',
    'days_sales_outstanding_trend',
    'cash_conversion_cycle'
);

UPDATE fact_metrics_recon_us
SET unit_type = 'DAYS'
WHERE metric_id IN (
    'days_sales_outstanding',
    'days_inventory_outstanding',
    'days_payable_outstanding',
    'days_sales_outstanding_trend',
    'cash_conversion_cycle'
);

UPDATE fact_metrics_recon_jp
SET unit_type = 'DAYS'
WHERE metric_id IN (
    'days_sales_outstanding',
    'days_inventory_outstanding',
    'days_payable_outstanding',
    'days_sales_outstanding_trend',
    'cash_conversion_cycle'
);

UPDATE fact_metrics_us
SET unit_type = CASE
    WHEN metric_id = 'goodwill_amortisation_addback' THEN 'CCY'
    WHEN metric_id = 'ifrs16_lease_interest_split' THEN 'CCY'
    ELSE 'DEC'
END
WHERE metric_id IN (
    'goodwill_amortisation_addback',
    'ifrs16_lease_interest_split',
    'ordinary_income_japan_gaap_metric',
    'special_gains_losses_japan_gaap_metric'
);

UPDATE fact_metrics_jp
SET unit_type = CASE
    WHEN metric_id = 'goodwill_amortisation_addback' THEN 'CCY'
    WHEN metric_id = 'ifrs16_lease_interest_split' THEN 'CCY'
    ELSE 'DEC'
END
WHERE metric_id IN (
    'goodwill_amortisation_addback',
    'ifrs16_lease_interest_split',
    'ordinary_income_japan_gaap_metric',
    'special_gains_losses_japan_gaap_metric'
);

UPDATE fact_metrics_recon_us
SET unit_type = CASE
    WHEN metric_id = 'goodwill_amortisation_addback' THEN 'CCY'
    WHEN metric_id = 'ifrs16_lease_interest_split' THEN 'CCY'
    ELSE 'DEC'
END
WHERE metric_id IN (
    'goodwill_amortisation_addback',
    'ifrs16_lease_interest_split',
    'ordinary_income_japan_gaap_metric',
    'special_gains_losses_japan_gaap_metric'
);

UPDATE fact_metrics_recon_jp
SET unit_type = CASE
    WHEN metric_id = 'goodwill_amortisation_addback' THEN 'CCY'
    WHEN metric_id = 'ifrs16_lease_interest_split' THEN 'CCY'
    ELSE 'DEC'
END
WHERE metric_id IN (
    'goodwill_amortisation_addback',
    'ifrs16_lease_interest_split',
    'ordinary_income_japan_gaap_metric',
    'special_gains_losses_japan_gaap_metric'
);
