//! Shared plumbing for the SignalGrid Rust services: message contracts,
//! NATS subject naming, configuration, telemetry and shutdown handling.

pub mod config;
pub mod model;
pub mod shutdown;
pub mod subjects;
pub mod telemetry;

pub use model::{RawEvent, Stats};
pub use shutdown::shutdown_signal;
