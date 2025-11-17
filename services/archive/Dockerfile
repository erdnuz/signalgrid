# Stage 1: Build dependencies
FROM rust:1.88 as builder
WORKDIR /usr/src/archive

# Copy dependency manifests only for caching
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release
RUN rm -rf src

# Stage 2: Build actual source
COPY src ./src
RUN cargo build --release

# Stage 3: Runtime
FROM debian:bookworm-slim  
RUN apt-get update && apt-get install -y ca-certificates libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /usr/local/bin
COPY --from=builder /usr/src/archive/target/release/archive .
EXPOSE 8003
CMD ["./archive"]
