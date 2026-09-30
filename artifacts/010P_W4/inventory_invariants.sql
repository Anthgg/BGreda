-- Read-only W4 inventory audit. Run against an isolated, sanitized fixture at Alembic 0045.
WITH prepared_lot_sums AS (
    SELECT
        b.product_id,
        b.location_id,
        b.quantity AS aggregate_quantity,
        COALESCE(SUM(l.quantity), 0) AS lot_quantity
    FROM stock_balances AS b
    JOIN products AS p ON p.id = b.product_id
    LEFT JOIN stock_lot_balances AS l
        ON l.product_id = b.product_id AND l.location_id = b.location_id
    WHERE p.product_type = 'PREPARED_MATERIAL'
    GROUP BY b.product_id, b.location_id, b.quantity
), latest_movements AS (
    SELECT id, product_id, location_id, movement_type, created_at, balance_after,
           ROW_NUMBER() OVER (
               PARTITION BY product_id, location_id ORDER BY created_at DESC, id DESC
           ) AS position
    FROM stock_movements
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
    SELECT
        COALESCE(i.production_order_id, r.production_order_id) AS production_order_id,
        COALESCE(i.product_id, r.product_id) AS product_id
    FROM production_in AS i
    FULL OUTER JOIN production_good AS r
        ON r.production_order_id = i.production_order_id AND r.product_id = i.product_id
    WHERE COALESCE(i.quantity, 0) <> COALESCE(r.quantity, 0)
)
SELECT jsonb_build_object(
    'generated_at_utc', to_char(timezone('UTC', now()), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'database', current_database(),
    'alembic_head', (SELECT version_num FROM alembic_version LIMIT 1),
    'stock_balance_rows', (SELECT COUNT(*) FROM stock_balances),
    'negative_stock_balances', (SELECT COUNT(*) FROM stock_balances WHERE quantity < 0),
    'lot_balance_rows', (SELECT COUNT(*) FROM stock_lot_balances),
    'negative_lot_balances', (SELECT COUNT(*) FROM stock_lot_balances WHERE quantity < 0),
    'prepared_aggregate_lot_mismatches', (
        SELECT COUNT(*) FROM prepared_lot_sums WHERE aggregate_quantity <> lot_quantity
    ),
    'lot_groups_without_aggregate', (
        SELECT COUNT(*) FROM (
            SELECT l.product_id, l.location_id
            FROM stock_lot_balances AS l
            LEFT JOIN stock_balances AS b
                ON b.product_id = l.product_id AND b.location_id = l.location_id
            WHERE b.id IS NULL
            GROUP BY l.product_id, l.location_id
        ) AS orphan_lots
    ),
    'movement_rows', (SELECT COUNT(*) FROM stock_movements),
    'movements_missing_required_origin', (
        SELECT COUNT(*) FROM stock_movements
        WHERE (movement_type IN ('PRODUCTION_OUT', 'PRODUCTION_IN') AND production_order_id IS NULL)
           OR (movement_type = 'PROTOTYPE_OUT' AND prototype_id IS NULL)
           OR (movement_type IN ('PREPARATION_OUT', 'PREPARATION_IN')
               AND preparation_id IS NULL AND source_preparation_id IS NULL)
    ),
    'last_movement_balance_mismatches', (
        SELECT COUNT(*)
        FROM latest_movements AS m
        LEFT JOIN stock_balances AS b
            ON b.product_id = m.product_id AND b.location_id = m.location_id
        WHERE m.position = 1 AND (b.id IS NULL OR m.balance_after <> b.quantity)
    ),
    'last_movement_balance_mismatch_details', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'movement_id', m.id,
            'movement_type', m.movement_type,
            'product_id', m.product_id,
            'location_id', m.location_id,
            'created_at', m.created_at,
            'movement_balance_after', m.balance_after::text,
            'stock_balance_quantity', b.quantity::text
        ) ORDER BY m.id)
        FROM latest_movements AS m
        LEFT JOIN stock_balances AS b
            ON b.product_id = m.product_id AND b.location_id = m.location_id
        WHERE m.position = 1 AND (b.id IS NULL OR m.balance_after <> b.quantity)
    ), '[]'::jsonb),
    'production_in_result_quantity_mismatches', (SELECT COUNT(*) FROM production_in_differences),
    'invalid_production_result_totals', (
        SELECT COUNT(*) FROM production_order_results
        WHERE started_quantity <> good_quantity + scrap_quantity
           OR started_quantity < 0 OR good_quantity < 0 OR scrap_quantity < 0
    ),
    'completed_v2_orders_without_results', (
        SELECT COUNT(*) FROM production_orders AS o
        WHERE o.status = 'COMPLETED'
          AND (o.v2_handoff_id IS NOT NULL OR o.v2_firing_handoff_id IS NOT NULL)
          AND NOT EXISTS (
              SELECT 1 FROM production_order_results AS r
              WHERE r.production_order_id = o.id
          )
    ),
    'delivery_out_invalid_rows', (
        SELECT COUNT(*) FROM stock_movements AS m
        LEFT JOIN production_orders AS o ON o.id = m.production_order_id
        WHERE m.movement_type = 'DELIVERY_OUT'
          AND (m.quantity >= 0 OR (
              m.production_order_id IS NOT NULL AND (
                  o.id IS NULL OR o.status <> 'COMPLETED' OR NOT EXISTS (
                      SELECT 1 FROM production_order_results AS r
                      WHERE r.production_order_id = o.id
                        AND r.product_id = m.product_id
                        AND r.good_quantity > 0
                  )
              )
          ))
    )
) AS inventory_invariants;
