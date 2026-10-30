# DataFlow: Metadata-Driven Reusable ETL Framework

A reusable, configuration-driven Python framework that extracts, validates, transforms, and loads
heterogeneous datasets into PostgreSQL, with data quality controls, auditability, operational
metrics, and idempotent processing.

> **Status: Phase 1 of 12 (foundation).** Configuration, Docker, and the test harness work. The
> pipeline CLI (`python -m src.main`) does not exist yet. This README only documents what exists
> and grows with each phase.

- Architecture and entity relationships: [docs/architecture.md](docs/architecture.md)
- Decisions and trade-offs: [docs/design_decisions.md](docs/design_decisions.md)
- AI prompt history: [prompts/claude_prompts.md](prompts/claude_prompts.md)

## Setup

Requires Python 3.12 and Docker with Compose.

```bash
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                # then set POSTGRES_PASSWORD in .env
docker compose up -d db             # PostgreSQL 18 on 127.0.0.1:5434
```

## Tests

```bash
pytest -v
pytest --cov=src --cov-report=term-missing
```

## Docker image

```bash
docker compose --profile job build pipeline
docker compose run --rm --no-deps --entrypoint python pipeline -m pytest tests/unit -q
```

The pipeline service is a job with a Compose profile, so `docker compose up -d` starts only the
database.

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED` during the image build.** Antivirus HTTPS scanning or a corporate proxy
is re-signing traffic with a root certificate that a clean container does not trust. Do not disable
verification. Export that root certificate in PEM format, outside this repository, and pass it as a
build secret. It is used for the install step only and is not stored in the image:

```bash
docker build --secret id=extra_ca,src=/path/to/root_ca.pem -t dataflow-pipeline .
```

Compose then uses this image for `docker compose run`.

## Naming conventions

`snake_case` tables and columns; ISO-8601 timestamps; UUID `run_id`; deterministic SHA-256 IDs for
derived records; `source_file` and `source_row_number` on every target and rejected row.
