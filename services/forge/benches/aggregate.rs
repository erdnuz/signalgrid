use criterion::{criterion_group, criterion_main, BatchSize, Criterion, Throughput};
use forge::WindowAggregator;
use signalgrid_core::RawEvent;
use std::hint::black_box;

const EVENTS: usize = 100_000;
const STATIONS: usize = 8;
const CHANNELS: usize = 4;

fn events() -> Vec<RawEvent> {
    (0..EVENTS)
        .map(|i| RawEvent {
            station: format!("Station{}", i % STATIONS),
            // 8 stations at 10 Hz each -> 80 events per second of event time
            timestamp: (i as i64 / STATIONS as i64) * 100,
            values: (0..CHANNELS)
                .map(|c| (i * CHANNELS + c) as f64 * 1e-3)
                .collect(),
            regime: Some((i % 3) as u8),
        })
        .collect()
}

fn bench_ingest(c: &mut Criterion) {
    let input = events();
    let mut group = c.benchmark_group("window_aggregator");
    group.throughput(Throughput::Elements(EVENTS as u64));
    group.bench_function("ingest_and_close_100k_events", |b| {
        b.iter_batched(
            || WindowAggregator::new(500, 200),
            |mut agg| {
                let mut emitted = 0;
                for (i, ev) in input.iter().enumerate() {
                    agg.ingest(ev);
                    if i % 40 == 0 {
                        emitted += agg.close_ready().len();
                    }
                }
                emitted += agg.close_all().len();
                black_box(emitted)
            },
            BatchSize::LargeInput,
        )
    });
    group.finish();
}

criterion_group!(benches, bench_ingest);
criterion_main!(benches);
