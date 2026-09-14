**English** · [Português](CHANGELOG.pt-BR.md)

# Changelog — Radar B2B

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.2.1] — 2026-09-14

Documentation made bilingual, with English as the portfolio-facing version.

### Changed

- `README.md`, `CHANGELOG.md` and `ENGINEERING_REVIEW.md` are now the English
  versions; `*.pt-BR.md` hold the Portuguese. Each carries a language switcher
  in the first line.
- The English README gained a **domain glossary** (CNPJ, CNAE, *competência*,
  Estabelecimentos/Empresas) and a short explanation of the two source
  properties that drive the architecture: monthly snapshots instead of a
  change log, and ~85 GB of messy, shard-split data. Without that context the
  design decisions are unreadable to anyone outside Brazil.
- Both READMEs were brought to parity and updated with what was learned since:
  shard instability across vintages, the `diagnostico_eventos.py` sanity gate,
  the two time/vintage anchoring rules, and the note on regression tests that
  cannot fail.
- `REVISAO_PD_V2.md` became `ENGINEERING_REVIEW.md` (+ `.pt-BR`). The old file
  is now a pointer and can be removed with `git rm`. References in
  `run_pipeline.py`, `scripts/carregar_icp.py` and `gld_icp_matches.sql` were
  updated.
- In the review, items 3.1, 3.3 and 3.6 are marked **RESOLVED in 2.2.0** —
  kept on the record, since the findings explain how the defects survived.

---

## [2.2.0] — 2026-09-14

Stage 1 of the review against the master plan (see `ENGINEERING_REVIEW.md`).
Three fixes that had to land **before** the full ten-shard load, because two of
them change what the load writes.

### Fixed — CRITICAL

- **The orchestrator never read `sys.argv`.** `run_pipeline.py` hardcoded
  everything in constants while the README documented a CLI and both GitHub
  Actions workflows used it. Python does not complain about arguments nobody
  consumes, so the options were **silently discarded**.

  Measured by running `main()` with the exact line from `ci.yml`
  (`--competencia 2026-08 --anterior 2026-07 --somente-local --sem-ia`): the
  pipeline ignored all four and ran 2024-07/2024-08 with all ten real shards.
  In practice, **every push to `main` triggered an attempt to download tens of
  gigabytes** from the tax authority, and the fixtures — the only real guard
  against parsing regressions — were never exercised. The monthly automation
  had the same defect: it discarded the `--competencia` input and would
  reprocess 2024-08 forever.
  → `argparse` with `--competencia`, `--anterior`, `--uf`, `--fatias`,
  `--icp`, `--somente-local` and `--sem-ia`, defaulting to the current
  constants: running with no arguments preserves the previous behaviour
  exactly. `--icp` now feeds dbt's `icp_ativo` var, which was previously
  always the one from `dbt_project.yml`.

### Fixed — HIGH

- **Silently ignored ICP keys.** `geografia.uf` and `eventos` existed in the
  YAML and `carregar_icp.py` never read them — the master plan §9 lists both
  as ICP attributes. Restricting by state in configuration had no effect at
  all.
  → New `config.icp_uf` and `config.icp_evento` tables, declared in
  `sources.yml` and applied in `gld_icp_matches`. An empty list means "no
  restriction", never "nothing passes".

### Changed — optimization

- **`empresas` restricted to the state filter.** The table has no state
  column, so without a filter Bronze would hold **~180 million rows per
  vintage** to serve ~2 million establishments in Minas Gerais. Since it is
  only used in a join by `cnpj_basico` against already-filtered
  establishments, it is now restricted to the surviving set.
  → Ingestion became **two sequential passes** (all establishments, then all
  companies) instead of interleaving tables shard by shard: there is no
  guarantee that `Empresas{i}` and `Estabelecimentos{i}` cover the same CNPJs,
  so the filter uses every establishment shard already written.
  → `_carregar_parquet` gained `filtro_extra`, combined with the state filter.

### Added

- 5 tests, each verified to fail against the previous version: the exact
  `ci.yml` line being honoured, `--icp` reaching dbt, defaults preserving
  no-argument behaviour, `uf`/`eventos` loaded from YAML, and `filtro_extra`
  composing with the state filter.

### Verification

| Check | Result |
|---|---|
| pytest | 45 passing (previously 40) |
| The 3 CLI/ICP tests against the previous version | fail, as expected |
| Lint (ruff) | clean |
| 3-shard ingestion with `--uf MG` | companies 350 → 287 (Minas Gerais CNPJs only) |

---

## [2.1.7] — 2026-09-14

With the 2.1.6 fixes the pipeline finally produced events: 580,729 for
2024-08. `diagnostico_eventos.py` rejected them: 95.5% were companies opened
years earlier, and the distribution by opening year reproduced the age profile
of the active company base — the signature of a sample, not of an event.

### Discovered

- **Shards are not stable across vintages.** Measured using the *same* shard 0
  in both vintages: **1,400,049 CNPJs (69.2%) exist only in 2024-07** and
  1,451,412 only in 2024-08 — of roughly 2 million rows each month, only ~30%
  are common. Company registrations do not evaporate like that.

  The same CNPJ moves between files on every publication. This invalidates the
  premise recorded in `config.py` ("partitioned by hash of the base CNPJ"):
  two vintages ingested with "shard 0" are different slices of the same
  universe, and the set difference between them measures sampling.

  Consequence: **there is no shortcut for change detection.** Measuring
  company formation requires all ten shards in both vintages.
  `assert_evento_nao_existe_na_competencia_anterior` does not protect against
  this — under its criterion the missing companies are legitimately absent.

### Changed — shard-by-shard ingestion

- Loading ten shards the old way (download everything, extract everything,
  then load) would require keeping **~45 GB of extracted CSV per vintage** on
  disk, over 100 GB for both.
  → `ingerir` now processes **one shard at a time**: download, extract, load,
  delete the CSV, then move on. Peak disk usage drops to a few GB. The ZIPs
  stay, since they are the cache that avoids re-downloading everything.
  → `_carregar_parquet` gained `fatia` (writes `dados_<n>.parquet` inside the
  partition) and `limpar` (only the first shard clears the folder). Hive
  partitioning reads every Parquet in the partition, so the result is
  identical to a single load.
  → The rejection threshold is evaluated per shard, while the CSV still
  exists — previously the denominator was computed afterwards, when the file
  would already have been deleted.

- **Partial-load warning.** Ingesting with fewer than all shards now prints an
  explicit warning that the result is good for exercising the plumbing and not
  for producing a digest.

- `run_pipeline.py`: `FATIAS` is now `0..9`, with the measurement justifying
  the decision recorded in the comment itself.

### Added

- 2 tests: one Parquet per shard summing to the partition total, and
  `limpar=False` preserving what was already loaded (with the inverse case, to
  pin the contract).

### Verification

| Check | Result |
|---|---|
| pytest | 40 passing (previously 38) |
| Lint (ruff) | clean |
| 3-shard ingestion (fixtures) | 3 Parquet in the partition, correct sum (861 = 3×287) |
| CSVs after load | folder empty — disk released |
| ZIPs after load | preserved (the `[SKIP]` cache) |

---

## [2.1.6] — 2026-09-14

The pipeline ran end to end, every stage marked ✅, digest generated — and
**zero events detected**. The entire product delivered a blank page without a
single error anywhere. Two independent defects, both from the same family: the
pipeline measured time by the wall clock instead of by the vintage being
processed, and the orchestrator never told dbt which vintage to process.

> Found by `scripts/diagnostico_eventos.py`. Without it, the natural
> conclusion would have been "no companies opened this month".

### Fixed — CRITICAL

- **dbt transformed a vintage that was not in the database.** The orchestrator
  ran `dbt run --profiles-dir .` **without `--vars`**, so the default from
  `dbt_project.yml` applied (`2026-08`/`2026-07`) while ingestion loaded
  `2024-08`/`2024-07`. Every `where competencia = '2026-08'` matched zero
  rows. All ten models reported OK because **building an empty table is a
  perfectly valid operation** — dbt has no way to know the emptiness was
  unexpected.

  The comment in `dbt_project.yml` already warned: *"override with --vars at
  run time."*
  → Vintages became constants at the top of `run_pipeline.py`, feeding both
  ingestion **and** dbt. One source, two consumers: it is now structurally
  impossible to ingest one month and transform another.

- **Recency measured against the execution date.** `gld_icp_matches` computed
  `date_diff('day', event_date, current_date)` and filtered on
  `dias_desde_evento <= recencia_dias` (45, per the ICP). Processing 2024-08 in
  September 2026, a company opened on 2024-08-05 scored **770 days** and was
  discarded. Even with `--vars` fixed, Gold would still have come out empty.
  → New `data_referencia()` macro: anchored to the last day of the vintage
  being processed. The same company now scores **26 days** and matches
  correctly.

  This also makes the calculation **idempotent** — reprocessing the same
  vintage a year from now yields the same score. It is precisely the defect
  the `evt_new_company` header already criticized in V3 ("the result changed
  depending on the day it ran") and which had survived in the Gold layer.

- **`confidence` always 'media'.** The same hardcoded `current_date` in
  `evt_new_company`: with historical data no event ever reached the 120-day
  window, so the field said the same thing about everything.
  → Now uses `data_referencia()`.

### Fixed — HIGH

- **Asymmetric shards between vintages.** `2024-07` was ingested with
  `--fatias 0` and `2024-08` with `--fatias 0,1`. Companies in shard 1 exist
  in both months **in reality**, but were only in one month's Bronze — so the
  set difference would classify them as new.
  `assert_evento_nao_existe_na_competencia_anterior` **does not catch this**.
  → `FATIAS` constant, shared by both vintages.

- **`dbt run` replaced by `dbt build`.** `run` does not execute the 24 data
  tests. The pipeline had been shipping a digest without ever running
  `assert_evento_nao_existe_na_competencia_anterior`, which the README calls
  the test without which "the product loses credibility". If a test fails now,
  the pipeline stops — the desired behaviour.

### Added

- `dbt_radar/macros/data_referencia.sql` — single documented time anchor.
- 3 orchestration tests that execute the real `main()` with `run_step`
  intercepted, validating the orchestration itself rather than a copy of it:
  consistent vintages between ingestion and dbt, identical shards, and use of
  `dbt build`. All three fail against the previous version.

### Changed

- `scripts/diagnostico_eventos.py` now treats **zero events as a failure**
  (exit code 1) rather than "nothing to analyse", and lists the probable
  causes in order. It also stopped printing `None -> None` when there are no
  events to derive the vintage from.
- `test_pipeline_carrega_icp_antes_do_dbt` no longer pins the `dbt run`
  subcommand — it broke on its own when we switched to `dbt build`.

### Verification

| Check | Result |
|---|---|
| pytest | 38 passing (previously 35) |
| The 3 orchestration tests against the previous version | fail, as expected |
| Lint (ruff) | clean |
| Generated dbt command | parses as valid YAML, correct vintages |
| Time anchor | 2024-08 → `2024-08-31`; a company from 2024-08-05 goes from 770 to 26 days |
| Diagnostic on an empty database | fails and names the right cause |

---

## [2.1.5] — 2026-09-14

Ingestion, ICP load and **the full dbt build** (`PASS=10, ERROR=0`, including
`evt_new_company`, `gld_icp_matches` and `gld_opportunities`). The pipeline
stopped at the next stage, on invocation form.

### Fixed — HIGH

- **Modules from `src/` invoked as loose files.** The orchestrator ran
  `python src/enriquecimento.py`, which loads the file outside the package and
  fails on the first relative import:
  `ImportError: attempted relative import with no known parent package`.
  `src/digest.py` has the same shape and would have failed right after.

  Ingestion was already invoked correctly (`python -m src.ingestao`) — the
  inconsistency was only in the last two stages, and the README had always
  documented the correct form.
  → `python -m src.enriquecimento` and `python -m src.digest`.

### Added

- Two regression tests, both verified against the previous version:
  - `test_pipeline_invoca_modulos_como_pacote` — no line of orchestrator code
    may invoke `python src/<module>.py`. It ignores comments, so the wrong
    form can be cited in the file's own documentation without failing the
    test.
  - `test_modulos_com_import_relativo_sao_chamados_com_dash_m` — parameterized
    per module: if the file uses `from .`, the orchestrator must call it with
    `python -m`. Automatically covers any new module under `src/`.

### Verification

| Check | Result |
|---|---|
| pytest | 35 passing (previously 31) |
| The 3 new tests against the previous version | fail, as expected |
| Lint (ruff) | clean |
| `python src/enriquecimento.py` | reproduces the ImportError |
| `python -m src.enriquecimento --help` | works |

> Without `GEMINI_API_KEY` the stage does not fail: the module warns
> (`[AVISO] GEMINI_API_KEY ausente`) and falls back to the heuristic, marking
> provenance — behaviour already specified in the README.

---

## [2.1.4] — 2026-09-14

With Bronze finally correct (2.0 M and 2.6 M establishments in Minas Gerais
across both vintages), the pipeline reached dbt and stopped at
`Catalog Error: Table with name "config.icp" does not exist`.

### Fixed — HIGH

- **The orchestrator skipped the ICP load.** `run_pipeline.py` went straight
  from ingestion to `dbt run`, but the README had always documented a step in
  between: `python scripts/carregar_icp.py`, which materializes `config.icp`,
  `config.icp_cnae`, `config.icp_municipio` and `config.icp_porte` — all read
  by `gld_icp_matches`.

  The pipeline had been working **by accident**: the table survived inside
  `data/radar.duckdb` from some earlier manual run. Recreating the database
  (necessary in 2.1.2 to discard contaminated Bronze) exposed the dependency
  on residual state — which would break identically on any clean machine, in
  CI, or after a `git clone`. This contradicts the master plan's **idempotent
  pipeline** requirement.
  → Step folded into the orchestrator, before dbt. It is idempotent
  (`CREATE OR REPLACE`), costs ~1 s, and keeps the ICP in sync with the YAML
  on every run instead of frozen at whatever was loaded once.

### Added

- `test_pipeline_carrega_icp_antes_do_dbt`: asserts that `run_pipeline.py`
  invokes `carregar_icp.py` **before** dbt. Verified to fail against the
  previous version.

### Changed

- `run_pipeline.py` passes ruff (9 pre-existing warnings: import ordering,
  whitespace, `result` and `e` assigned but unused). The README already
  required it to be linted.
- Stages renumbered to 1A, 1B, 2 … 5.

### Verification

| Check | Result |
|---|---|
| pytest | 31 passing (previously 30) |
| New test against the previous version | fails, as expected |
| Lint (ruff), now including `run_pipeline.py` | clean |
| `carregar_icp.py` on a database without the schema | creates all 4 tables (1 ICP, 9 CNAEs, 3 municipalities, 2 sizes) |

---

## [2.1.3] — 2026-09-14

2.1.2 declared `encoding='latin-1'` throughout the pipeline — what the
handbook prescribes and what most documentation about the CNPJ open data
repeats. It was **incomplete**: the load started aborting with
`Invalid Input Error: File is not latin-1 encoded` on `Estabelecimentos`.

### Fixed — CRITICAL

- **The source is not latin-1; it is Windows-1252.** The files carry bytes
  `0x80–0x9F`, a range ISO-8859-1 reserves for control characters and
  Windows-1252 uses for curly quotes (`" "`), en dashes (`–`), ellipses (`…`)
  and apostrophes (`'`) — residue of text typed on Windows, routine in trade
  names. Lookup tables (controlled vocabulary) contain no such bytes;
  free-text files do.

  DuckDB's CSV reader cannot resolve this in any configuration:

  | Attempt | Result |
  |---|---|
  | `encoding='latin-1'` | aborts: *"File is not latin-1 encoded"* |
  | `encoding='latin-1'` + `ignore_errors` | aborts identically — it is a hard failure, not a row rejection |
  | `encoding='cp1252'` / `'windows-1252'` / `'iso-8859-1'` | *"The CSV Reader does not support the encoding"* (only utf-8, utf-16, latin-1) |
  | `encoding='utf-8'` | internal error and then an **invalidated connection** |

  → Normalization moved into **extraction** (`extrair`): content leaves the
  ZIP converted from cp1252 to **UTF-8**, and the whole pipeline reads UTF-8
  from there on. Since extraction already writes the file, converting in
  streaming costs **no additional I/O**. cp1252 is single-byte, so no
  character crosses a chunk boundary.

  Side benefit: `" "` and `–` are **preserved** as characters instead of
  becoming `?` or being swapped for ASCII quotes — which, since `"` is the CSV
  quote character, would corrupt the file structure itself.

### Changed

- `extrair` now writes the member under its **base name** instead of
  recreating the ZIP's internal tree. The tax authority's files are flat; as a
  bonus this removes any *zip slip* risk.
- `ENCODING_RFB` now means "how the pipeline writes and reads" (`utf-8`);
  `ENCODING_FONTE` (`cp1252`) describes the origin.

### Added

- **Fixtures are now written in cp1252**, with one CNAE containing a curly
  quote and an en dash. Verified: against 2.1.2 these fixtures reproduce the
  exact error (*"File is not latin-1 encoded"*); against this version they
  load with the characters preserved.
- 2 tests: `extrair` returns valid UTF-8 with the right characters, and the
  full ZIP → extraction → load path with C1 bytes.

### Verification

| Check | Result |
|---|---|
| pytest | 30 passing (previously 28) |
| Lint (ruff) | clean |
| cp1252 fixtures against 2.1.2 | fail with the original error — regression covered |
| End-to-end ingestion with `--uf MG` | 287 establishments, 14 CNAEs |
| Characters preserved | `Comércio de peças "genuínas" – sob encomenda` |

> Diagnosis backed by sweeping all 256 byte values through DuckDB's reader:
> under `latin-1` it rejects exactly the `0x80–0x9F` range.

---

## [2.1.2] — 2026-09-14

The load with **real data** was breaking on `Estabelecimentos` and — worse —
**succeeding with wrong data** on the other tables. Three independent parsing
defects, all from the same root cause: the code read the tax authority's files
in a format that is not theirs.

> **Process regression.** The fixes described in 2.1.1 (line-by-line reading
> of lookup tables; `store_rejects`) **were no longer in the code**:
> `src/ingestao.py` was rewritten on 13/09 and lost them, while the CHANGELOG
> kept claiming they existed. The test suite raised nothing — see *Fixed —
> tests that could not fail*, below.

### Fixed — CRITICAL

- **Wrong encoding: accented rows vanished silently.** The files are `latin-1`
  (handbook §2.1, "source gotchas"), but the load declared no encoding and
  DuckDB assumed UTF-8. Accented bytes are invalid UTF-8 sequences; with
  `ignore_errors=true`, **the entire row was discarded without warning**. On
  the real CNAE file: **145 of 1,359 rows loaded** — exactly the 145 that
  contain no accent at all. A missing CNAE is a company that stops matching
  the ICP.
  → Explicit `encoding='latin-1'` on every read.

- **`quote=''`: quotes entered the data and the state filter never matched.**
  Fields come wrapped in double quotes (`"0111301";"Cultivo de arroz"`). With
  quoting disabled, the value read was `"MG"` — quotes included — and
  `WHERE column19 = 'MG'` matched nothing. That was the cause of
  `estabelecimentos  0 linhas` for an entire vintage, with no error
  whatsoever. The other tables loaded "successfully" with **every value
  polluted by quotes** (`"ALPHA LTDA"`, `"0,00"`).
  → `quote='"'`, which also stops a `;` **inside** a field
  (`"LOJA A; LOJA B"`) from becoming an extra column — the origin of the
  `Error when sniffing file ... ESTABELE` / *"columns are set as 30, sniffer
  found 31"* that was halting ingestion.

- **Rejection counter hardcoded to zero.** `_carregar_parquet` ended with
  `return inseridas, 0`. The entire `LIMIAR_REJEICAO` guard — which should
  abort the load when the layout changes — was **dead code**: no rejection was
  ever counted, so the ratio never exceeded 0%.
  → `store_rejects=true` with real counting. It counts **distinct rows**
  (`COUNT(DISTINCT (file_id, line))`) rather than error records: DuckDB emits
  one error per excess column, and summing those would inflate the ratio
  enough to trip the threshold on a healthy base.

### Fixed — HIGH

- **The sniffer aborted whole files.** Even with the correct dialect, DuckDB
  still tried to infer it and failed when bad rows confused the sample —
  taking down the entire shard instead of rejecting the rows.
  → `auto_detect=false`. Delimiter, quoting, encoding and columns are already
  declared; there is nothing to guess. The worst case becomes "everything
  rejected and counted" (diagnosable) rather than an opaque exception.

- **Lookup tables read line by line again** (the 2.1.1 technique, lost in the
  rewrite): `\x07` delimiter and split on the first `;`. Deliberately without
  `ignore_errors` — here, losing a row means losing a CNAE.

- **`reject_scans` collision between tables.** `store_rejects` materializes two
  tables; naming only the error one made the second call on the same
  connection fail with *"Reject Scan Table name reject_scans is already in
  use"*.
  → `rejects_scan` named per table.

- **Rejection threshold measured against the wrong denominator.** It compared
  rejected against *loaded*; with `--uf MG`, "loaded" counts only Minas Gerais
  while rejections come from the whole country, so the ratio is meaningless.
  → Now measured against the file's total line count, computed only **when
  there is at least one rejection** (zero cost on the healthy path).

### Fixed — tests that could not fail

The suite passed **21/21 against thoroughly broken code**. Two tests were
decorative:

- `test_dominio_com_ponto_e_virgula_na_descricao` reimplemented the SQL
  **inside the test itself**, validating the technique and not the production
  code. When `ingestao.py` lost the technique, the test stayed green.
- `test_limiar_de_rejeicao_configurado` only checked the **constant's value**
  (`0 < LIMIAR <= 0.05`), never that rejections were counted — it passed
  happily with `return inseridas, 0`.

→ 7 new tests that call the **production functions** and fail against the
previous code: lookup row preserved, accent preserved, `;` inside a field,
state filter with a quoted value, rejection counted, one bad row equals one
rejection, and a fully malformed file not aborting on sniffing.

### Added

- **Fixtures with accents.** The fixtures were pure ASCII ("TRES CORACOES",
  "Comercio"), and in ASCII `latin-1` and UTF-8 are byte-for-byte identical —
  which is why CI passed while real data broke. Municipalities, CNAEs and
  trade names now carry accents, and ~5% of trade names carry a `;` inside the
  field. A comment in the file explains that the accents are not decoration.

### Verification

| Check | Result |
|---|---|
| pytest | 28 passing (previously 21) |
| The 7 new tests against the previous code | 7 failing, as expected |
| Lint (ruff) | clean |
| Real CNAE file (`F.K03200$Z.D40810.CNAECSV`) | 1,359/1,359 rows read (previously 145) |
| Accents on real data | `Produção de sementes certificadas` intact |
| `;` inside a field | `LOJA A; LOJA B` preserved, no extra column |
| End-to-end ingestion with `--uf MG` | 289 establishments (previously 0) |

> CNAE counts measured against the real file already downloaded under
> `data/raw/2024-08/`. The complete production load (all ten shards) remains
> pending a window in which the tax authority's infrastructure cooperates.

---

## [2.1.1] — 2026-09-08

First execution with **real data**. The download of all 22 files worked
(confirming the 2.1.0 fix); the load broke on parsing.

### Fixed — CRITICAL

- **Lookup files aborted the entire load.** The CSV parser failed with
  `Error when sniffing file ... CNAECSV` / *"columns are set as 2, sniffer
  found 3"*.
  **Root cause:** the files are inconsistent — some rows come without quotes,
  and some descriptions contain `;`. A row such as

  ```
  4618401;Representantes comerciais; agentes do comercio
  ```

  makes the parser see 3 fields and abort reading the whole file.
  → We now read the **entire line as a single field** (delimiter `\x07`,
  absent from the file) and split on the **first `;`**. Since the code never
  contains `;`, everything to the right is the description — no matter how
  many semicolons it holds or whether it is quoted. **Zero rows lost.**
  We discarded `ignore_errors=true`: it would drop the row silently, and
  losing a CNAE means companies stop matching the ICP.

### Fixed — HIGH

- **Wide files had no tolerance for a malformed row.** `Estabelecimentos`
  (30 columns) has the same class of defect, and a single bad row would take
  down the whole shard.
  → `ignore_errors=true` + **`store_rejects=true`**: bad rows no longer bring
  the load down, but they also **do not vanish silently** — they are counted
  and displayed. Above **1%** rejection the ingestion fails with a message
  pointing at the layout dictionary, because at that point the problem is not
  "a bad row" but "the layout changed".

- **Vintage assumed from the calendar.** `run_pipeline.py` used the current
  month as the current vintage. Since the tax authority publishes with weeks
  of lag, on 08/09 the pipeline tried to process `2026-09`, not yet published.
  → The orchestrator now **discovers** the latest published vintage and
  validates that both current and previous exist in the share before starting,
  listing what is available when they do not.

### Added

- Synthetic fixtures now reproduce the source's **real defects**: descriptions
  with `;`, internal quotes, and 1 in 3 rows without quotes. CI would now
  catch that regression on its own.
- 2 regression tests (dirty parsing without row loss; rejection threshold).
- Per-table rejected-row report in the ingestion output.

### Verification

| Check | Result |
|---|---|
| pytest | 21 passing (previously 19) |
| dbt tests | 34 passing |
| Lint (ruff) | clean |
| Pathological CNAEs | 12/12 read, `;` preserved in the description |
| End-to-end pipeline | OK |

> Note: in a description with an unescaped quote (`peças 1" e 2"`), the
> trailing quote is removed — an ambiguity unsolvable without knowing the
> source's escaping rule. The row is preserved, which is what matters.

---

## [2.1.0] — 2026-09-08

Data-source correction. Every address in the project was verified against what
is actually online.

### Fixed — CRITICAL

- **Download host decommissioned.** The project pointed at
  `dadosabertos.rfb.gov.br/CNPJ/dados_abertos_cnpj/AAAA-MM/` and variants on
  `arquivos.receitafederal.gov.br/.../dados_abertos_cnpj/`. **At the end of
  January 2026 the tax authority migrated publication** to a WebDAV
  (Nextcloud) share, and the old paths ceased to exist. That explains why no
  mirror responded and why `Estabelecimentos` ingestion never completed.
  → New `src/receita.py` module talks to the current share:
  - listing via `PROPFIND` on
    `arquivos.receitafederal.gov.br/public.php/webdav`;
  - download at
    `arquivos.receitafederal.gov.br/public.php/dav/files/<token>/<AAAA-MM>/<file>`.

- **Vintage guessing replaced by discovery.** The previous version scanned up
  to 12 months testing URLs to find which existed — wasted requests and a
  source of false negatives.
  → The pipeline now **lists** what has been published. `--competencia` became
  optional (default: the most recent), and `python -m src.ingestao --listar`
  shows available vintages and files.

- **Community mirror removed.** The previous version fell back, as a last
  resort, to a GitHub mirror frozen at `2024.09`. An event-detection pipeline
  that silently uses two-year-old data produces an entire digest of fake "new
  companies".
  → Removed. Explicit failure beats stale data disguised as current.

### Changed

- **Configurable share token.** `RADAR_RFB_SHARE_TOKEN` allows swapping the
  token without editing code, should the tax authority publish a new share.
  The error message explains how to obtain it.
- **Actionable diagnostics.** Listing failures distinguish "host down" from
  "invalid/changed token", with specific instructions for each.
- **README** gained a table of official addresses, a note about the January
  2026 migration, and the token-rotation procedure.
- `src/config.py` centralizes the addresses, including the
  [dados.gov.br](https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj)
  catalogue and the
  [layout dictionary](https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf).

### Added

- 5 regression tests: vintage and file extraction from the WebDAV XML,
  exclusion of non-ZIP entries from listings, download URL format, and — via
  AST — the guarantee that no module again **points** at the decommissioned
  hosts (comments and docstrings may cite them to document the migration).

### Verification

| Check | Result |
|---|---|
| Lint (ruff) | clean |
| pytest | 19 passing (previously 14) |
| dbt tests | 34 passing |
| End-to-end pipeline | OK |
| WebDAV parser | validated against Nextcloud-format XML |

> Downloading real data still depends on the availability of the tax
> authority's infrastructure, historically unstable. The addresses are now
> correct; the first real load remains pending.

---

## [2.0.0] — 2026-09-07

Structural refactor following a full code audit. The goal was to close the gap
between what the master plan **declares** and what the repository
**implemented**: several non-negotiable principles (idempotence, "the AI never
invents", decoupled ICP, explainable score with factors) were written in the
document but absent from the code.

Prior state: the pipeline had never run with real data. The versioned database
contained 3 fictional establishments (`11111111`, `22222222`, `33333333`), and
the generated digest was entirely synthetic.

### Fixed — CRITICAL

- **Fabricated content presented as AI analysis.** `gerar_mock()` returned
  invented text ("Firm focused on accounting and tax advisory") with a
  hardcoded `confianca_ia: "Alta"`, rendered by the digest under the heading
  "Commercial Intelligence (AI Context)", indistinguishable from real output.
  It violated principles 6 and 7 of the master plan and posed a direct
  reputational risk during client validation.
  → Every enriched row now carries `origem` (`llm` | `heuristica`) and
  `modelo`. The digest stamps provenance on each opportunity and displays a
  warning at the top when any item lacks AI interpretation. The fallback
  stopped imitating analysis: it declares "not inferred" on the fields that
  depend on the model.

- **AI failures silently masked.** A generic `except Exception` caught any
  error (invalid key, rate limit, retired model, timeout) and replaced it with
  invented text, with no count and no alert.
  → Failures are now counted, logged and reported. Above a 30% failure rate
  the run stops with an actionable message instead of producing a degraded
  digest.

- **No diff between vintages — the Event Store did not actually exist.**
  Ingestion used `CREATE OR REPLACE TABLE`, with no vintage column and no
  partitioning: there was a single snapshot, overwritten on every run.
  `evt_new_company` detected new companies via
  `data_inicio_atividade >= current_date - 30`, the shortcut the handbook had
  explicitly rejected. Consequences: results varied by run date (not
  idempotent); companies entered with retroactive dates were lost; and there
  was no foundation for any other event in the catalogue.
  → Vintage became a first-class citizen. Bronze writes
  `data/bronze/<table>/competencia=AAAA-MM/*.parquet`. `evt_new_company` uses
  a real **set difference** between current and previous vintage — the same
  mechanic that will support `STATUS_CHANGED`, `PARTNER_*` and the rest.

- **Incomplete ingestion.** It downloaded only `Estabelecimentos0.zip` (1 of
  10 shards, ~10% of the country) — insufficient for a municipal ICP, and
  capable of emptying the digest.
  → Downloads all 10 shards of `Estabelecimentos` and `Empresas`, with an
  optional state filter for development (`--uf MG`).

### Fixed — HIGH

- **Inert ICP filter (`ilike '%ti%'`).** The pattern matched
  "A-**TI**-vidades", present in a large share of Brazilian sector
  descriptions, plus "cosmé**ti**cos" and "Par**ti**cipação". In practice the
  sector filter did not filter: the ICP was "any company in Varginha".
  → Matching is now by **CNAE code**, with primary, secondary and **excluded**
  lists (an accounting firm should not prospect another accounting firm). A
  regression test in `tests/test_pipeline.py` documents the bug.

- **`empresas` table missing — digest without company names.** The product
  identified opportunities only as `CNPJ 11111111000199`, unusable for the end
  user. It also blocked the `porte` and `capital` score factors.
  → `slv_empresas` added, bringing legal name, share capital and size.
  `nome_fantasia` (column 04), previously discarded, is now ingested too.

- **Score with only 3 possible values.** The formula was a fixed base of 50
  plus recency (35/15/0), yielding 50, 65 or 85 — with no discrimination
  inside a band. The "+50 ICP base" was constant for everyone who passed, so
  it scored nothing. `where opportunity_score >= 50` was logically inert.
  → Six weighted factors (recency with linear decay, CNAE fit, size,
  location, share capital, contact completeness), with weights from the ICP.
  Verified distribution: 16 distinct values on the test set.

- **Score not auditable.** The explanation was a concatenated string with no
  underlying values.
  → `score_fatores` persisted as structured JSON; the displayed text is
  derived from it. A data test guarantees the factors sum exactly to the
  displayed score.

- **ICP hardcoded in SQL.** `municipio_nome = 'VARGINHA'` embedded in the
  model — every new client would require editing SQL.
  → ICP lives in `config/icp/*.yaml`, loaded into configuration tables by
  `scripts/carregar_icp.py`. New client = new YAML file.

- **Project not reproducible.** No `requirements.txt`, `pyproject.toml` or
  `environment.yml` — impossible to clone and run.
  → `pyproject.toml` with dependencies, extras (`ia`, `dev`) and
  `ruff`/`pytest` configuration.

- **`.gitignore.txt`** — the `.txt` extension made Git ignore the ignore file
  itself. → Renamed to `.gitignore`.

- **CI broken by construction.** The workflow was GitHub's default template:
  conda-based, referencing a non-existent `environment.yml`, running `pytest`
  with no tests — failing on every push.
  → Replaced by `ci.yml` (lint + tests + **end-to-end pipeline on synthetic
  fixtures**) and `pipeline-mensal.yml` (scheduled run).

### Fixed — MEDIUM

- **`verify=False` on every download**, disabling TLS verification.
  → Verification on by default; disabling requires an explicit
  `RADAR_TLS_INSECURE=1`.

- **Outdated SDK and model.** `google.generativeai` (legacy) with
  `gemini-1.5-flash` (retired) — the real call was probably already failing,
  and failing silently into fabricated text.
  → Migrated to `google-genai`, model configurable via `RADAR_LLM_MODEL`
  (default `gemini-2.0-flash`).

- **Enrichment without cache.** Every run did `DROP TABLE` and reprocessed
  everything, re-spending tokens — contradicting "precomputed, never pay twice
  for the same company".
  → Persistent table keyed by `event_id`; only previously unseen
  opportunities are sent to the model.

- **Prompt without grounding.** It did not forbid invention, did not receive
  the ICP or the score/factors, and asked for `dor_provavel` as though it were
  established fact.
  → `system_instruction` with explicit rules; ICP, score and factors now form
  the context; the field was renamed `hipotese_de_dor` and is presented as a
  sector-level hypothesis, not a fact about the company.

- **Inconsistent schemas.** dbt wrote everything to `main` (the `profiles.yml`
  defined no schema) while the Python script wrote to `gold`; the
  `silver/`/`gold/` folders were purely cosmetic.
  → Schemas configured per layer in `dbt_project.yml`.

- **Zero data tests.** `tests/` empty, no `schema.yml`.
  → 24 dbt tests (`unique`, `not_null`, `accepted_values`) plus 5 singular
  tests, including the Event Store's central check: no `NEW_COMPANY` event may
  reference a CNPJ already present in the previous vintage.

- **No observability** (master plan §16).
  → `src/observabilidade.py` logs stage, vintage, status, counts, LLM calls,
  duration and failures to `meta.run_log`.

- **No feedback loop** (§13).
  → `gold.opportunity_feedback` table and marking fields on every digest
  opportunity.

- **`extrair_csv` assumed a single file per ZIP** (`namelist()[0]`).
  → Extracts all members.

- **Download without integrity check.** Existence was enough; a truncated ZIP
  passed as valid.
  → `_zip_integro()` validates via `testzip()`; corrupted files are
  re-downloaded. Covered by tests.

- **Fragile orchestrator.** It used a literal `python` with `shell=True`,
  breaking outside the active venv; it took no vintage; it ran no tests.
  → `sys.executable` with an argument list; vintage parameterized and
  propagated to dbt via `--vars`; uses `dbt build` (models + tests), so bad
  data stops the pipeline before it becomes a digest.

- **Outdated README**, describing V1 and a roadmap ending at BI.
  → Rewritten for V2.

### Added

- `scripts/gerar_fixtures.py` — generates synthetic data in the tax
  authority's exact format (30 columns, `latin-1`, `;`), across two vintages,
  allowing the pipeline to be developed and tested without the ~85 GB download
  or the instability of the official mirrors. Also used by CI.
- `src/config.py` — official layout and paths centralized. A layout change at
  the source becomes a one-line edit rather than a hunt for indices scattered
  through SQL.
- `src/observabilidade.py` — structured run log.
- `dbt_radar/models/bronze/` — explicit Bronze layer reading the partitioned
  Parquet via `hive_partitioning`.
- `slv_dominios` — CNAEs and municipalities deduplicated by most recent
  vintage.
- Priority bands (high/medium/low) in the digest, per master plan §12.
- `confidence` field in the Event Store: distinguishes a recent opening from a
  retroactive entry or registry correction.

### Verification

Pipeline executed end to end with two synthetic vintages (400 and 460
establishments, 60 companies exclusive to the newer vintage):

| Check | Result |
|---|---|
| Set difference | 58 events detected (60 new − 2 inactive, correctly filtered by `situacao_cadastral`) |
| dbt tests | 34 passing, 0 errors |
| pytest | 14 passing |
| Lint (ruff) | clean |
| Idempotence | two full runs → identical `event_id` values |
| Enrichment cache | second run: 0 LLM calls |
| Score distribution | 16 distinct values (previously 3) |

### Known limitations

- The pipeline **had not yet run with real data** — the official mirrors did
  not respond during the refactor. Ingestion is ready and parameterized; the
  first real load is pending.
- The event catalogue remains `NEW_COMPANY` only, by scope decision. The
  foundation (vintage + partitioned Parquet) already supports the rest.
- SCD-2 snapshots not implemented: only needed for attribute-change events.
- Score weights are initial hypotheses, to be calibrated with real conversion
  data.
- E-mail delivery remains manual (concierge), per the MVP.

---

## [1.0.0] — 2026-08-25

- Initial version: ingestion of `Estabelecimentos0`, dbt models
  (`slv_estabelecimentos`, `evt_new_company`, `gld_icp_matches`,
  `gld_opportunities`), Gemini enrichment and Markdown digest generation. End
  to end chain working on sample data.
