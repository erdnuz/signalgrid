# Rust runs in the pinned toolchain image, so no local install is needed.
RUST = docker run --rm -v "$(CURDIR):/repo" -v sg-cargo:/usr/local/cargo/registry \
       -v sg-target:/target -e CARGO_TARGET_DIR=/target/ws -w /repo/services rust:1.88
PY_PACKAGES = services/forecast frontend

.PHONY: up down clean logs test lint smoke bench

up:      ## build and start everything
	cp -n .env.example .env 2>/dev/null || true
	docker compose up -d --build

down:    ## stop, keep data
	docker compose down

clean:   ## stop and delete all data (Postgres, JetStream)
	docker compose down -v

logs:
	docker compose logs -f --tail 50

test:
	$(RUST) cargo test --workspace
	for p in $(PY_PACKAGES); do (cd $$p && pytest -q) || exit 1; done

lint:
	$(RUST) sh -c "rustup component add clippy rustfmt >/dev/null && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings"
	for p in $(PY_PACKAGES); do (cd $$p && ruff check . && ruff format --check . && mypy) || exit 1; done

smoke:   ## end-to-end delivery test against a running stack
	python3 scripts/smoke_test.py

bench:
	$(RUST) cargo bench -p forge --bench aggregate
