use crate::QueryParams;
use serde::Serialize;
use signalgrid_core::Stats;
use sqlx::migrate::Migrator;
use sqlx::postgres::PgPoolOptions;
use sqlx::{FromRow, PgPool, Postgres, QueryBuilder};
use std::time::Duration;
use tracing::{error, info, warn};

pub static MIGRATOR: Migrator = sqlx::migrate!("./migrations");

#[derive(Debug, Clone, PartialEq, Serialize, FromRow)]
pub struct StatsRow {
    pub station: String,
    pub sensor: i32,
    pub timestamp: i64,
    pub window_ms: i32,
    pub mean: f64,
    pub min: f64,
    pub max: f64,
    pub count: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub regime: Option<i16>,
}

/// Connects with exponential backoff and applies pending migrations.
pub async fn connect(url: &str) -> anyhow::Result<PgPool> {
    let mut backoff = Duration::from_secs(1);
    let pool = loop {
        match PgPoolOptions::new()
            .max_connections(5)
            .acquire_timeout(Duration::from_secs(5))
            .connect(url)
            .await
        {
            Ok(pool) => break pool,
            Err(e) => {
                error!(event = "db_connect_failed", error = %e, backoff_secs = backoff.as_secs());
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(30));
            }
        }
    };
    MIGRATOR.run(&pool).await?;
    info!(event = "db_ready");
    Ok(pool)
}

/// Columnar form of a batch, bound as Postgres arrays so the whole batch is a
/// single round trip (`INSERT ... SELECT FROM UNNEST(...)`).
#[derive(Default)]
struct Columns {
    station: Vec<String>,
    sensor: Vec<i32>,
    timestamp: Vec<i64>,
    window_ms: Vec<i32>,
    mean: Vec<f64>,
    min: Vec<f64>,
    max: Vec<f64>,
    count: Vec<i64>,
    regime: Vec<Option<i16>>,
}

impl Columns {
    fn push(&mut self, s: &Stats) -> Result<(), std::num::TryFromIntError> {
        let sensor = i32::try_from(s.sensor)?;
        let window_ms = i32::try_from(s.window_ms)?;
        let count = i64::try_from(s.count)?;
        self.station.push(s.station.clone());
        self.sensor.push(sensor);
        self.timestamp.push(s.timestamp);
        self.window_ms.push(window_ms);
        self.mean.push(s.mean);
        self.min.push(s.min);
        self.max.push(s.max);
        self.count.push(count);
        self.regime.push(s.regime.map(i16::from));
        Ok(())
    }
}

const INSERT_BATCH_SQL: &str = "
    INSERT INTO stats (station, sensor, timestamp, window_ms, mean, min, max, count, regime)
    SELECT * FROM UNNEST(
        $1::text[], $2::int[], $3::bigint[], $4::int[],
        $5::float8[], $6::float8[], $7::float8[], $8::bigint[], $9::smallint[]
    )
    ON CONFLICT (station, sensor, timestamp) DO NOTHING";

/// Idempotently inserts a batch; returns the number of rows actually written
/// (redelivered windows are skipped by the primary key).
pub async fn insert_batch(pool: &PgPool, stats: &[Stats]) -> sqlx::Result<u64> {
    let mut cols = Columns::default();
    for s in stats {
        if let Err(e) = cols.push(s) {
            warn!(event = "row_out_of_range", id = %s.message_id(), error = %e);
        }
    }
    if cols.station.is_empty() {
        return Ok(0);
    }

    let result = sqlx::query(INSERT_BATCH_SQL)
        .bind(cols.station)
        .bind(cols.sensor)
        .bind(cols.timestamp)
        .bind(cols.window_ms)
        .bind(cols.mean)
        .bind(cols.min)
        .bind(cols.max)
        .bind(cols.count)
        .bind(cols.regime)
        .execute(pool)
        .await?;
    Ok(result.rows_affected())
}

/// Returns the most recent `limit` rows matching the filters, oldest first.
pub async fn query_stats(pool: &PgPool, q: &QueryParams) -> sqlx::Result<Vec<StatsRow>> {
    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
        "SELECT station, sensor, timestamp, window_ms, mean, min, max, count, regime FROM stats WHERE TRUE",
    );
    if let Some(station) = &q.station {
        qb.push(" AND station = ").push_bind(station);
    }
    if let Some(sensor) = q.sensor {
        qb.push(" AND sensor = ").push_bind(sensor);
    }
    if let Some(start) = q.start_ts {
        qb.push(" AND timestamp >= ").push_bind(start);
    }
    if let Some(end) = q.end_ts {
        qb.push(" AND timestamp <= ").push_bind(end);
    }
    qb.push(" ORDER BY timestamp DESC LIMIT ")
        .push_bind(q.limit);

    let mut rows = qb.build_query_as::<StatsRow>().fetch_all(pool).await?;
    rows.reverse();
    Ok(rows)
}
