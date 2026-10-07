# syntax=docker/dockerfile:1
# One build for every Rust service in the workspace. All binaries are compiled
# by a single cargo invocation in a shared builder stage; each service image
# is a tiny runtime stage that copies its binary out:
#   docker build --target forge -t signalgrid/forge services/
# cargo-chef caches compiled dependencies in a layer keyed only on the
# dependency graph, so source edits rebuild just the workspace crates.
#
# CARGO_PROFILE=release (default) is the optimised production build (thin LTO,
# one codegen unit). CI passes CARGO_PROFILE=ci, which skips LTO to build fast.

FROM rust:1.88-slim-bookworm AS chef
RUN cargo install cargo-chef --locked --version 0.1.71
WORKDIR /app

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
ARG CARGO_PROFILE=release
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --profile "$CARGO_PROFILE" --locked --recipe-path recipe.json
COPY . .
RUN cargo build --profile "$CARGO_PROFILE" --locked -p pulse -p forge -p archive \
    && mkdir -p /out \
    && for bin in pulse forge archive; do cp "target/$CARGO_PROFILE/$bin" /out/; done

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home signalgrid
USER 10001

FROM runtime AS pulse
COPY --from=builder /out/pulse /usr/local/bin/app
ENTRYPOINT ["/usr/local/bin/app"]

FROM runtime AS forge
COPY --from=builder /out/forge /usr/local/bin/app
ENTRYPOINT ["/usr/local/bin/app"]

FROM runtime AS archive
COPY --from=builder /out/archive /usr/local/bin/app
ENTRYPOINT ["/usr/local/bin/app"]
