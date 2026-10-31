"""Build reports/output_report.html, one page showing the results of the latest runs.

Every figure comes from the files in reports/ or a live database query, so nothing is typed by hand.

Usage:
    python scripts/build_output_report.py
"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import load_settings  # noqa: E402
from src.database.connection import create_db_engine  # noqa: E402

REPORTS = ROOT / "reports"

STYLE = """
:root {
  /* Layout: a single reading column of evidence tables, summary first, detail after. */
  --paper: #f4f6f5; --panel: #ffffff; --ink: #17242a; --quiet: #586870; --rule: #d6dfdd;
  --accent: #0b6e69; --good: #17694a; --good-bg: #dff3e8; --warn: #8a5a00; --warn-bg: #fbeed0;
  --bad: #a4262c; --bad-bg: #fbe3e4;
  --display: "IBM Plex Sans Condensed", "Arial Narrow", Arial, sans-serif;
  --body: "IBM Plex Sans", "Segoe UI", Arial, sans-serif;
  --mono: "IBM Plex Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; --paper: #111a1d; --panel: #18242a; --ink: #e6eeec; --quiet: #9fb0b3; --rule: #2d3d43;
  --accent: #5cc8bf; --good: #7fd8ad; --good-bg: #173427; --warn: #ecc36f; --warn-bg: #3a2d10; --bad: #f19a9e; --bad-bg: #3f1c1f;
} }
:root[data-theme="dark"] {
  color-scheme: dark; --paper: #111a1d; --panel: #18242a; --ink: #e6eeec; --quiet: #9fb0b3; --rule: #2d3d43;
  --accent: #5cc8bf; --good: #7fd8ad; --good-bg: #173427; --warn: #ecc36f; --warn-bg: #3a2d10; --bad: #f19a9e; --bad-bg: #3f1c1f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.5 var(--body); }
main { max-width: 1040px; margin: 0 auto; padding: 32px 20px 64px; display: flex; flex-direction: column; gap: 36px; }
h1, h2, h3 { font-family: var(--display); text-wrap: balance; line-height: 1.15; margin: 0; }
h1 { font-size: 2.2rem; }
h2 { font-size: 1.5rem; padding-bottom: 8px; border-bottom: 2px solid var(--accent); }
h3 { font-size: 1.08rem; margin-top: 18px; }
p { margin: 8px 0 0; max-width: 72ch; }
.meta { color: var(--quiet); font-size: .92rem; }
section { display: flex; flex-direction: column; gap: 10px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--rule); border-top: 3px solid var(--good); padding: 12px 14px; }
.tile b { display: block; font: 600 1.55rem/1.2 var(--display); font-variant-numeric: tabular-nums; }
.tile span { color: var(--quiet); font-size: .88rem; }
.scroll { overflow-x: auto; background: var(--panel); border: 1px solid var(--rule); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { padding: 7px 11px; border-bottom: 1px solid var(--rule); text-align: left; vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { color: var(--quiet); font-weight: 600; white-space: nowrap; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code { font: .86em var(--mono); overflow-wrap: anywhere; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: .8rem; font-weight: 600; white-space: nowrap; }
.good { color: var(--good); background: var(--good-bg); }
.warn { color: var(--warn); background: var(--warn-bg); }
.bad { color: var(--bad); background: var(--bad-bg); }
"""

GOOD = {"match", "pass", "success", "caught", "passed", "ok"}
WARN = {"skipped", "success_with_rejects", "not_comparable"}


def esc(value: object) -> str:
    return html.escape("" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value))


def pill(value: str) -> str:
    kind = "good" if value in GOOD else "warn" if value in WARN else "bad"
    return f'<span class="pill {kind}">{esc(value)}</span>'


def number(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return esc(value)


def table(headers: list[str], rows: list[list[object]], numeric: set[int] = frozenset(), raw: set[int] = frozenset()) -> str:
    head = "".join(f'<th{" class=n" if i in numeric else ""}>{esc(h)}</th>' for i, h in enumerate(headers))
    body = ""
    for row in rows:
        cells = ""
        for i, value in enumerate(row):
            content = str(value) if i in raw else number(value)
            cells += f'<td{" class=n" if i in numeric else ""}>{content}</td>'
        body += f"<tr>{cells}</tr>"
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def load(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def tests_summary() -> tuple[str, str]:
    lines = (REPORTS / "test_results.txt").read_text(encoding="utf-8").splitlines()
    passed = re.search(r"(\d+) passed", lines[-1]).group(1)
    coverage = next(line.split()[-1] for line in lines if line.startswith("TOTAL"))
    return passed, coverage


def reconciliation_section(dataset: str, title: str) -> str:
    report = load(f"reconciliation_{dataset}_source.json")
    tables = report["reconciliation"]["tables"]
    rows = [[f"<code>{esc(name)}</code>", t["source_rows"], t["valid_rows"], t["rejected_rows"], t["collapsed_duplicates"],
             t["flagged_duplicates"], t["target_rows_total"]] for name, t in tables.items()]
    totals = [[f"<code>{esc(a['name'])}</code>", a["source_valid_total"], a["target_total"], a["difference"], pill(a["status"])]
              for t in tables.values() for a in t["aggregates"]]
    checks = [[f"<code>{esc(c['name'])}</code>", c["description"], c["violations"], pill(c["status"])]
              for c in report["reconciliation"]["checks"]]
    warnings = [[f"<code>{esc(name)}</code>", f"<code>{esc(rule)}</code>", count, "loaded and flagged"]
                for name, t in tables.items() for rule, count in t["warnings_by_rule"].items()]
    rejected = [[f"<code>{esc(name)}</code>", f"<code>{esc(rule)}</code>", count, "quarantined"]
                for name, t in tables.items() for rule, count in t["rejected_by_rule"].items()]
    performance = report["performance"]
    return (
        f"<h3>{esc(title)}</h3>"
        f"<p class=meta>Full load: {performance['total_seconds']:,.1f}s, {performance['throughput_rows_per_second']:,.0f} rows per second, "
        f"peak memory {performance['peak_rss_mb']:,.0f} MB. Status: {pill(report['status'])}</p>"
        + table(["Table", "Source rows", "Valid", "Rejected", "Collapsed", "Flagged copies", "Rows in target"], rows, {1, 2, 3, 4, 5, 6}, {0})
        + (table(["Total", "Source", "Target", "Difference", "Status"], totals, {1, 2, 3}, {0, 4}) if totals else "")
        + table(["SQL check", "What it confirms", "Violations", "Status"], checks, {2}, {0, 3})
        + table(["Table", "Rule", "Rows", "Outcome"], rejected + warnings, {2}, {0, 1})
    )


def query_sections(engine) -> str:
    blocks = re.split(r"^-- name: ", (ROOT / "docs" / "sample_queries.sql").read_text(encoding="utf-8"), flags=re.M)[1:]
    parts = []
    with engine.connect() as conn:
        for block in blocks:
            title, _, sql = block.partition("\n")
            frame = pd.read_sql(text(sql), conn)
            numeric = {i for i, c in enumerate(frame.columns) if pd.api.types.is_numeric_dtype(frame[c]) or frame[c].map(lambda v: hasattr(v, "as_tuple")).any()}
            rows = [[v.item() if hasattr(v, "item") else (float(v) if hasattr(v, "as_tuple") else v) for v in row]
                    for row in frame.itertuples(index=False)]
            rows = [[(str(v).split(".")[0] if "duration" in str(frame.columns[i]) else
                      (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v)) for i, v in enumerate(r)] for r in rows]
            parts.append(f"<h3>{esc(title.strip())}</h3>" + table([c.replace("_", " ") for c in frame.columns], rows, numeric))
    return "".join(parts)


def performance_section() -> str:
    rows = []
    for dataset in ("hotel_bookings", "olist"):
        for r in load(f"performance_{dataset}.json")["results"]:
            rows.append([dataset, r["scenario"].replace("_", " "), r["requested_size"], r["records"], r["total_seconds_median"],
                         r["loading_seconds_median"], r["records_per_second_median"], r["peak_rss_mb_median"]])
    return table(["Dataset", "Scenario", "Size", "Records", "Total s", "Load s", "Records/s", "Peak MB"], rows, {2, 3, 4, 5, 6, 7})


def main() -> None:
    settings = load_settings()
    engine = create_db_engine(settings.database)
    passed, coverage = tests_summary()
    mutation = load("mutation_check.json")
    verification = load("independent_verification.json")
    hotel = load("reconciliation_hotel_bookings_source.json")["totals"]
    olist = load("reconciliation_olist_source.json")["totals"]
    docker = (REPORTS / "docker_execution.txt").read_text(encoding="utf-8")
    docker_tests = re.search(r"(\d+) passed", docker).group(1)
    with engine.connect() as conn:
        rejected = conn.execute(text(
            "SELECT source_file, source_row_number, rejection_reason, original_record->>'adr' AS adr, "
            "original_record->>'adults' AS adults, original_record->>'children' AS children, original_record->>'babies' AS babies "
            "FROM ops.rejected_records ORDER BY rejection_reason, source_row_number LIMIT 6")).all()
    tiles = [
        (f"{passed} passed", f"Automated tests, {coverage} coverage"),
        (f"{mutation['caught']} of {mutation['mutations']}", "Deliberate bugs caught by the tests"),
        (f"{verification['checks'] - verification['failed']} of {verification['checks']}", "Figures matching the raw files, checked separately"),
        (f"{hotel['input']:,}", f"Hotel rows in; {hotel['valid']:,} loaded, {hotel['rejected']:,} rejected"),
        (f"{olist['input']:,}", f"Olist rows in; {olist['valid']:,} unique rows loaded, {olist['rejected']:,} rejected"),
        (f"{docker_tests} passed", "Tests inside the Docker container"),
    ]
    tile_html = "".join(f"<div class=tile><b>{esc(a)}</b><span>{esc(b)}</span></div>" for a, b in tiles)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DataFlow ETL Output</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap" rel="stylesheet">
<style>{STYLE}</style></head><body><main>
<header><h1>DataFlow ETL Output</h1>
<p>Results of the metadata-driven ETL framework on two real datasets: Olist e-commerce and Hotel Booking Demand, loaded into PostgreSQL 18.</p>
<p class="meta">Built {datetime.now().strftime("%Y-%m-%d %H:%M")} local time from the project's result files and a live database query.</p></header>
<section><h2>Summary</h2><div class="tiles">{tile_html}</div></section>
<section><h2>Reconciliation: source against target</h2>
<p>Source totals are read from the raw text before validation. A difference in a total would roll the load back.</p>
{reconciliation_section("hotel_bookings", "Hotel bookings")}{reconciliation_section("olist", "Olist")}</section>
<section><h2>Rejected records keep their source and original values</h2>
<p>A sample from quarantine. Every rejected row can be traced to its file and row number.</p>
{table(["File", "Row", "Reason", "adr", "adults", "children", "babies"], [list(r) for r in rejected], {1})}</section>
<section><h2>Query results</h2>{query_sections(engine)}</section>
<section><h2>Performance</h2>
<p>Medians of 3 runs on synthetic data, in a separate database, on one shared laptop. Use these to compare, not as capacity figures. Size means bookings for hotel and orders for Olist, whose related tables add up to about 7.6 records per order.</p>
{performance_section()}</section>
<section><h2>Sources and licences</h2>
<p>Olist, <em>Brazilian E-Commerce Public Dataset</em>, CC BY-NC-SA 4.0. Antonio, Almeida &amp; Nunes (2019), <em>Hotel booking demand datasets</em>, via Jesse Mostipak on Kaggle, CC BY 4.0. This page shows aggregates and rejected-row samples only.</p></section>
</main></body></html>
"""
    engine.dispose()
    output = REPORTS / "output_report.html"
    output.write_text(page, encoding="utf-8")
    print(f"Wrote {output} ({len(page):,} characters)")


if __name__ == "__main__":
    main()
