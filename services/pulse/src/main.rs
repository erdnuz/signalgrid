use async_nats::ConnectOptions;
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::time::{Duration};
use uuid::Uuid;
use warp::Filter;
use rand::Rng;
use chrono::Utc;
use rand_distr::StandardNormal;

#[derive(Debug, Serialize, Deserialize, Clone)]
enum Sensor {
    Temperature,
    Pressure,
    Humidity,
    Wind,
    Aqi,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Event {
    id: String,
    station: String,
    sensor: Sensor,
    timestamp: u64,
    value: f64,
}

#[derive(Clone)]
struct StationProfile {
    temp_offset: f64,
    pressure_offset: f64,
    humidity_offset: f64,
    wind_scale: f64,
}

struct EnvState {
    last_pressure: f64,
    stations: Vec<(String, StationProfile)>,
}

impl EnvState {
    fn new() -> Self {
        EnvState {
            last_pressure: 1013.0,
            stations: vec![
                (
                    "Station_A".to_string(),
                    StationProfile {
                        temp_offset: -2.0,
                        pressure_offset: 1.5,
                        humidity_offset: 5.0,
                        wind_scale: 0.8,
                    },
                ),
                (
                    "Station_B".to_string(),
                    StationProfile {
                        temp_offset: 1.0,
                        pressure_offset: -1.0,
                        humidity_offset: -3.0,
                        wind_scale: 1.2,
                    },
                ),
            ],
        }
    }

    fn step(&mut self, profile: &StationProfile, rng: &mut impl Rng) -> (f64, f64, f64, f64, f64) {
        let temp: f64 = 15.0 + profile.temp_offset + rng.sample::<f64, _>(StandardNormal) * 0.4;
        let pressure_drift = -0.5 * (temp - 15.0);
        let pressure: f64 = self.last_pressure + profile.pressure_offset + pressure_drift + rng.sample::<f64, _>(StandardNormal) * 20.0;
        self.last_pressure = pressure;
        let humidity: f64 = (80.0 + profile.humidity_offset - 0.5 * (temp - 15.0) - 0.2 * (pressure - 1013.0))
            .max(0.0)
            .min(100.0)
            + rng.sample::<f64, _>(StandardNormal) * 2.0;
        let wind: f64 = profile.wind_scale * ((pressure_drift.abs() * 5.0) + 1.0) + rng.sample::<f64, _>(StandardNormal) * 0.5;
        let aqi: f64 = 50.0 + (100.0 - humidity) * 0.2 - wind * 2.0 + rng.sample::<f64, _>(StandardNormal) * 5.0;
        (temp, pressure, humidity, wind, aqi)
    }
}

fn gen_event(state: &mut EnvState) -> Event {
    let mut rng = rand::rng();
    let idx = rng.random_range(0..state.stations.len());
    let (station_name, profile) = state.stations[idx].clone();

    let (temp, pressure, humidity, wind, aqi) = state.step(&profile, &mut rng);

    let sensor_types = [
        Sensor::Temperature,
        Sensor::Pressure,
        Sensor::Humidity,
        Sensor::Wind,
        Sensor::Aqi,
    ];
    let sensor = sensor_types[rng.random_range(0..sensor_types.len())].clone();

    let value = match sensor {
        Sensor::Temperature => temp,
        Sensor::Pressure => pressure,
        Sensor::Humidity => humidity,
        Sensor::Wind => wind,
        Sensor::Aqi => aqi,
    };

    Event {
        id: Uuid::new_v4().to_string(),
        station: station_name,
        sensor,
        timestamp: Utc::now().timestamp_millis() as u64,
        value,
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let nats_url = "nats://nats:4222";
    let nc = ConnectOptions::new().connect(nats_url).await?;

    println!("Pulse connected to NATS at {}", nats_url);

    let last_event: Arc<tokio::sync::Mutex<Option<Event>>> = Arc::new(tokio::sync::Mutex::new(None));
    let mut env_state = EnvState::new();
    let last_event_clone = last_event.clone();

    // Publisher task
    println!("Starting event publisher...");
    tokio::spawn(async move {
        loop {
            let event = gen_event(&mut env_state);

            let payload: Bytes = match serde_json::to_vec(&event) {
                Ok(vec) => vec.into(),
                Err(e) => {
                    eprintln!("Failed to serialize event: {:?}", e);
                    continue;
                }
            };

            if let Err(e) = nc.publish("events", payload).await {
                eprintln!("Failed to publish to NATS: {:?}", e);
            }

            {
                let mut guard = last_event_clone.lock().await;
                *guard = Some(event.clone());
            }

            // Random delay 100–500 ms
            let delay_milli = rand::random_range(100..=500);
            tokio::time::sleep(Duration::from_millis(delay_milli)).await;
        }
    });

    // HTTP endpoint
    let last_event_filter = warp::any().map({
        let last_event = last_event.clone();
        move || last_event.clone()
    });
    let fetch_route = warp::path("events")
        .and(last_event_filter)
        .and_then(|last_event: Arc<tokio::sync::Mutex<Option<Event>>>| async move {
            let event = last_event.lock().await;
            Ok::<_, warp::Rejection>(warp::reply::json(&*event))
        })
        .with(warp::cors().allow_any_origin());

    println!("HTTP server running at http://0.0.0.0:8001");
    warp::serve(fetch_route).run(([0, 0, 0, 0], 8001)).await;

    Ok(())
}
