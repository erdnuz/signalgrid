//! Runs against a real Postgres when `TEST_DATABASE_URL` is set (CI provides
//! one); otherwise the tests are skipped.

use archive::{db, QueryParams};
use signalgrid_core::Stats;

async fn pool() -> Option<sqlx::PgPool> {
    let url = std::env::var("TEST_DATABASE_URL").ok()?;
    Some(db::connect(&url).await.expect("connect + migrate"))
}

fn stat(station: &str, sensor: u32, ts: i64, mean: f64) -> Stats {
    Stats {
        station: station.into(),
        sensor,
        timestamp: ts,
        window_ms: 500,
        mean,
        min: mean - 1.0,
        max: mean + 1.0,
        count: 5,
        regime: Some(0),
    }
}

fn query(station: &str) -> QueryParams {
    QueryParams {
        station: Some(station.into()),
        sensor: None,
        start_ts: None,
        end_ts: None,
        limit: 1_000,
    }
}

#[tokio::test]
async fn insert_batch_is_idempotent() {
    let Some(pool) = pool().await else { return };
    let station = format!("Idem{}", std::process::id());
    let batch: Vec<Stats> = (0..10)
        .map(|i| stat(&station, 0, i * 500, i as f64))
        .collect();

    assert_eq!(db::insert_batch(&pool, &batch).await.unwrap(), 10);
    // Redelivery of the same windows writes nothing.
    assert_eq!(db::insert_batch(&pool, &batch).await.unwrap(), 0);
    assert_eq!(
        db::query_stats(&pool, &query(&station))
            .await
            .unwrap()
            .len(),
        10
    );
}

#[tokio::test]
async fn query_returns_latest_rows_oldest_first() {
    let Some(pool) = pool().await else { return };
    let station = format!("Order{}", std::process::id());
    let batch: Vec<Stats> = (0..20)
        .map(|i| stat(&station, 1, i * 500, i as f64))
        .collect();
    db::insert_batch(&pool, &batch).await.unwrap();

    let rows = db::query_stats(
        &pool,
        &QueryParams {
            limit: 5,
            ..query(&station)
        },
    )
    .await
    .unwrap();
    let ts: Vec<i64> = rows.iter().map(|r| r.timestamp).collect();
    assert_eq!(ts, vec![7_500, 8_000, 8_500, 9_000, 9_500]);
}
