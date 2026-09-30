-- 010P post-migration inventory gate. READ ONLY; do not run during prep.
-- Connect to the release database only after maintenance lock and migration.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '30s';

WITH prepared_lot_sums AS (
    SELECT b.product_id, b.location_id, b.quantity AS aggregate_quantity,
           COALESCE(SUM(l.quantity), 0) AS lot_quantity
    FROM stock_balances AS b
    JOIN products AS p ON p.id = b.product_id
    LEFT JOIN stock_lot_balances AS l
      ON l.product_id = b.product_id AND l.location_id = b.location_id
    WHERE p.product_type = 'PREPARED_MATERIAL'
    GROUP BY b.product_id, b.location_id, b.quantity
), production_in AS (
    SELECT production_order_id, product_id, SUM(quantity) AS quantity
    FROM stock_movements
    WHERE movement_type = 'PRODUCTION_IN'
    GROUP BY production_order_id, product_id
), production_good AS (
    SELECT production_order_id, product_id, SUM(good_quantity) AS quantity
    FROM production_order_results
    WHERE product_id IS NOT NULL
    GROUP BY production_order_id, product_id
), production_in_differences AS (
    SELECT COALESCE(i.production_order_id, r.production_order_id) AS production_order_id,
           COALESCE(i.product_id, r.product_id) AS product_id
    FROM production_in AS i
    FULL OUTER JOIN production_good AS r
      ON r.production_order_id = i.production_order_id AND r.product_id = i.product_id
    WHERE COALESCE(i.quantity, 0) <> COALESCE(r.quantity, 0)
), movement_sequence AS (
    SELECT m.*,
           b.quantity AS current_balance,
           SUM(m.quantity) OVER (
               PARTITION BY m.product_id, m.location_id
           ) AS total_movement_quantity,
           COALESCE(SUM(m.quantity) OVER (
               PARTITION BY m.product_id, m.location_id
               ORDER BY m.created_at, m.id
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
           ), 0) AS prior_movement_quantity
    FROM stock_movements AS m
    LEFT JOIN stock_balances AS b
      ON b.product_id = m.product_id AND b.location_id = m.location_id
), result_origins AS (
    SELECT production_order_id, 'legacy:' || production_order_line_id::text AS origin
    FROM production_order_results WHERE production_order_line_id IS NOT NULL
    UNION ALL
    SELECT production_order_id, 'v2:' || v2_quotation_product_id::text AS origin
    FROM production_order_results WHERE v2_quotation_product_id IS NOT NULL
    UNION ALL
    SELECT production_order_id, 'firing:' || v2_firing_quotation_line_id::text AS origin
    FROM production_order_results WHERE v2_firing_quotation_line_id IS NOT NULL
), duplicate_result_origins AS (
    SELECT production_order_id, origin
    FROM result_origins
    GROUP BY production_order_id, origin
    HAVING COUNT(*) > 1
), orphan_movements AS (
    SELECT m.id
    FROM stock_movements AS m
    LEFT JOIN products AS p ON p.id = m.product_id
    LEFT JOIN stock_locations AS l ON l.id = m.location_id
    LEFT JOIN units_of_measure AS u ON u.code = m.uom_code
    LEFT JOIN recipe_preparations AS prep ON prep.id = m.preparation_id
    LEFT JOIN recipe_preparations AS source_prep ON source_prep.id = m.source_preparation_id
    LEFT JOIN prototypes AS proto ON proto.id = m.prototype_id
    LEFT JOIN production_orders AS po ON po.id = m.production_order_id
    LEFT JOIN v2_quotations AS q ON q.id = m.v2_quotation_id
    LEFT JOIN import_batches AS batch ON batch.id = m.import_batch_id
    WHERE p.id IS NULL OR l.id IS NULL OR u.code IS NULL
       OR (m.preparation_id IS NOT NULL AND prep.id IS NULL)
       OR (m.source_preparation_id IS NOT NULL AND source_prep.id IS NULL)
       OR (m.prototype_id IS NOT NULL AND proto.id IS NULL)
       OR (m.production_order_id IS NOT NULL AND po.id IS NULL)
       OR (m.v2_quotation_id IS NOT NULL AND q.id IS NULL)
       OR (m.import_batch_id IS NOT NULL AND batch.id IS NULL)
), invariant_counts AS (
    SELECT
      (SELECT COUNT(*) FROM stock_balances WHERE quantity < 0) AS negative_balances,
      (SELECT COUNT(*) FROM stock_lot_balances WHERE quantity < 0) AS negative_lot_balances,
      (SELECT COUNT(*) FROM prepared_lot_sums WHERE aggregate_quantity <> lot_quantity)
        AS lot_reconciliation_discrepancies,
      (SELECT COUNT(*) FROM (
          SELECT l.product_id, l.location_id
          FROM stock_lot_balances AS l
          LEFT JOIN stock_balances AS b
            ON b.product_id = l.product_id AND b.location_id = l.location_id
          WHERE b.id IS NULL
          GROUP BY l.product_id, l.location_id
      ) AS missing_aggregate) AS lot_groups_without_aggregate,
      (SELECT COUNT(*) FROM orphan_movements) AS orphan_movements,
      (SELECT COUNT(*) FROM duplicate_result_origins) AS duplicate_origin_application,
      (SELECT COUNT(*) FROM movement_sequence
       WHERE current_balance IS NULL
          OR balance_after <> current_balance - total_movement_quantity
                                  + prior_movement_quantity + quantity)
        AS movement_sequence_balance_mismatches,
      (SELECT COUNT(*) FROM production_in_differences) AS production_in_result_mismatches,
      (SELECT COUNT(*) FROM production_order_results
       WHERE started_quantity <> good_quantity + scrap_quantity
          OR started_quantity < 0 OR good_quantity < 0 OR scrap_quantity < 0)
        AS invalid_production_result_totals,
      (SELECT COUNT(*) FROM movement_sequence
       WHERE movement_type = 'DELIVERY_OUT'
         AND (current_balance IS NULL OR quantity >= 0
              OR ABS(quantity) > current_balance - total_movement_quantity
                                               + prior_movement_quantity
              OR balance_after < 0))
        AS delivery_exceeds_available,
      (SELECT COUNT(*) FROM stock_movements AS m
       WHERE (m.movement_type IN ('PRODUCTION_OUT', 'PRODUCTION_IN')
              AND m.production_order_id IS NULL)
          OR (m.movement_type = 'PROTOTYPE_OUT' AND m.prototype_id IS NULL)
          OR (m.movement_type IN ('PREPARATION_OUT', 'PREPARATION_IN')
              AND m.preparation_id IS NULL AND m.source_preparation_id IS NULL))
        AS movements_missing_required_origin
)
SELECT jsonb_build_object(
    'generated_at_utc', to_char(timezone('UTC', now()), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'database', current_database(),
    'alembic_head', (SELECT version_num FROM alembic_version LIMIT 1),
    'alembic_head_rows', (SELECT COUNT(*) FROM alembic_version),
    'alembic_head_is_0045', (
      (SELECT COUNT(*) FROM alembic_version) = 1
      AND (SELECT MIN(version_num) FROM alembic_version) = '0045'
    ),
    'negative_balances', negative_balances,
    'negative_lot_balances', negative_lot_balances,
    'lot_reconciliation_discrepancies', lot_reconciliation_discrepancies,
    'lot_groups_without_aggregate', lot_groups_without_aggregate,
    'orphan_movements', orphan_movements,
    'duplicate_origin_application', duplicate_origin_application,
    'movement_sequence_balance_mismatches', movement_sequence_balance_mismatches,
    'production_in_result_mismatches', production_in_result_mismatches,
    'invalid_production_result_totals', invalid_production_result_totals,
    'delivery_exceeds_available', delivery_exceeds_available,
    'movements_missing_required_origin', movements_missing_required_origin,
    'all_invariants_zero', (
      negative_balances = 0 AND negative_lot_balances = 0
      AND lot_reconciliation_discrepancies = 0 AND lot_groups_without_aggregate = 0
      AND orphan_movements = 0 AND duplicate_origin_application = 0
      AND movement_sequence_balance_mismatches = 0
      AND production_in_result_mismatches = 0 AND invalid_production_result_totals = 0
      AND delivery_exceeds_available = 0 AND movements_missing_required_origin = 0
      AND (SELECT COUNT(*) FROM alembic_version) = 1
      AND (SELECT MIN(version_num) FROM alembic_version) = '0045'
    )
) AS post_0045_invariants
FROM invariant_counts;

ROLLBACK;
