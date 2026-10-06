# Signal Grid

**Signal Grid** is a microservice-based data pipeline for simulating, processing, forecasting, and visualizing sensor data. It provides real-time statistics, forecasts with confidence intervals, and a web dashboard for monitoring.

---

## Architecture Overview

For a detailed description of the system and each service's responsibilities, see [Architecture.md](./Architecture.md).

The application consists of several services communicating via **NATS**:

- **Pulse (Rust)**: Simulates OU (Ornstein-Uhlenbeck) sensor data with regime changes and publishes to NATS.  
- **Forge (Rust)**: Subscribes to the data, computes statistics (mean, min, max), and republishes results to NATS.  
- **Archive (Rust)**: Subscribes to stats and archives them in a PostgreSQL database. Provides an API to query historical data by station, sensor, and time range.  
- **Forecast (Python)**: Subscribes to data and stats, produces forecasts with confidence intervals, and publishes forecast results.  
- **Frontend (Python / Dash)**: Dash-based web dashboard to visualize live and forecasted sensor data. Users pick a station and sensor to see live means alongside 3-step forecasts with 98% confidence intervals.

---

## Directory Structure

```text
signal-grid/
├── docker-compose.yml
├── frontend/ # Python
├── init.sql
├── README.md
└── services/
    ├── archive/ # Rust
    ├── forecast/ # Python
    ├── forge/ # Rust
    └── pulse/ # Rust
```

---

## Installation

### Prerequisites

- Docker & Docker Compose installed  
- Git

### Clone the repository

```bash
git clone https://github.com/erdnuz/signalgrid
cd signalgrid
```

### Start the services

```bash
cp .env.example .env   # local dev credentials
docker compose up --build
```

This will:

1. Launch **NATS** server for inter-service messaging.
2. Start **PostgreSQL** for archival storage.
3. Run all services: `pulse`, `forge`, `forecast`, `archive`, and `frontend`.

### Access the frontend

Open your browser at: [http://localhost:8004](http://localhost:8004)

- Use the dropdowns to select a station and sensor.
- View live measurements, forecasts, and confidence intervals.
