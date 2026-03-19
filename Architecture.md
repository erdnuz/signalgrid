# SignalGrid Architecture

## Overview

SignalGrid is a modular, containerized platform for ingesting, processing, forecasting, and archiving time-series sensor data. The system is composed of several microservices, each with a focused responsibility, communicating via NATS messaging and sharing a unified data pipeline.

---

## Service Breakdown

### 1. **frontend**

- **Language:** Python (Dash, Plotly)
- **Role:** Provides a web dashboard for real-time visualization of sensor data and forecasts. Users can select stations and sensors, view live actuals vs. forecast plots, and interact with the system.
- **Key Features:**
  - Live updating graphs
  - Dropdowns for station/sensor selection
  - Consumes processed and forecasted data from the pipeline

### 2. **forecast**

- **Language:** Python
- **Role:** Performs time-series forecasting on incoming sensor data. Listens for new data events, applies statistical or ML models, and publishes forecast results (with confidence intervals) back to the pipeline.
- **Key Features:**
  - Receives raw/processed sensor data
  - Runs forecasting models
  - Publishes forecast and confidence intervals

### 3. **archive**

- **Language:** Rust
- **Role:** Responsible for long-term storage and retrieval of sensor data. Listens for data events and persists them efficiently. Supports querying historical data for analysis or reprocessing.
- **Key Features:**
  - High-performance data ingestion
  - Efficient storage (e.g., using columnar or compressed formats)
  - Query API for historical data

### 4. **pulse**

- **Language:** Rust
- **Role:** Simulates or ingests live sensor data into the system. Can act as a data generator for testing or as a real-world data collector.
- **Key Features:**
  - Generates or ingests time-series events
  - Publishes events to the pipeline

### 5. **forge**

- **Language:** Rust
- **Role:** Performs data aggregation, transformation, or enrichment. Listens for raw sensor events, applies business logic, and emits processed data for downstream services (e.g., forecast, archive).
- **Key Features:**
  - Data cleaning, aggregation, or feature engineering
  - Publishes processed data

---

## Data Pipeline & Communication

- **Message Bus:** All services communicate via NATS (lightweight, high-performance messaging system).
- **Event Flow:**
  1. **pulse** generates or ingests sensor data and publishes to NATS.
  2. **forge** subscribes to raw events, processes/aggregates, and republishes.
  3. **archive** and **forecast** subscribe to processed events:
     - **archive** stores them for long-term retrieval.
     - **forecast** runs models and publishes forecast results.
  4. **frontend** subscribes to both actual and forecast events for live visualization.

---

## Diagram

```mermaid
graph TD
    Pulse[Pulse (Rust): Data Ingest/Sim]
    Forge[Forge (Rust): Aggregation/Transform]
    Archive[Archive (Rust): Storage]
    Forecast[Forecast (Python): Forecasting]
    Frontend[Frontend (Python): Dashboard]
    NATS[NATS Message Bus]

    Pulse -- Raw Events --> NATS
    NATS -- Raw Events --> Forge
    Forge -- Processed Events --> NATS
    NATS -- Processed Events --> Archive
    NATS -- Processed Events --> Forecast
    Forecast -- Forecast Events --> NATS
    NATS -- Actual/Forecast Events --> Frontend
```

---

## Summary Table

| Service   | Language | Role/Responsibility                |
|-----------|----------|------------------------------------|
| frontend  | Python   | Dashboard & visualization          |
| forecast  | Python   | Forecasting & confidence intervals |
| archive   | Rust     | Long-term storage & retrieval      |
| pulse     | Rust     | Data ingestion/simulation          |
| forge     | Rust     | Aggregation & transformation       |

---

## Extensibility

- The architecture is modular: new services can be added by subscribing/publishing to NATS topics.
- Each service is independently deployable and testable.
