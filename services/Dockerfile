# syntax=docker/dockerfile:1
# Shared build for every Rust service in the workspace:
#   docker build --build-arg SERVICE=forge -t signalgrid/forge services/
# cargo-chef caches compiled dependencies in a layer keyed only on the
# dependency graph, so source edits rebuild just the workspace crates.

FROM rust:1.88-slim-bookworm AS chef
RUN cargo install cargo-chef --locked --version 0.1.71
WORKDIR /app

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --release --locked --recipe-path recipe.json
COPY . .
# Declared after `cook` so the dependency layer is shared across services.
ARG SERVICE
RUN test -n "$SERVICE" && cargo build --release --locked -p "$SERVICE" \
    && cp "target/release/$SERVICE" /usr/local/bin/app

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home signalgrid
COPY --from=builder /usr/local/bin/app /usr/local/bin/app
USER 10001
ENTRYPOINT ["/usr/local/bin/app"]
