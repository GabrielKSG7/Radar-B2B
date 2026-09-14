**English** · [Português](README.pt-BR.md)

# Radar B2B

> Detect change. Find opportunity. Act first.

A business-event intelligence engine built on Brazil's public company
registry. It watches the universe of companies for change, turns that change
into **structured events**, matches them against an **Ideal Customer Profile
(ICP)**, and produces **prioritized, explainable sales opportunities**.

The product does not sell raw data, lead lists, or company lookup. It sells
**context, priority, opportunity and action**.

```
DATA → EVENT → CONTEXT → ICP → SCORE → OPPORTUNITY → ACTION → FEEDBACK
```

---

## The data, for readers outside Brazil

Brazil's federal tax authority (*Receita Federal*) publishes the full national
company registry as open data every month. Some context that shapes every
design decision in this repository:

| Term | What it is |
|---|---|
| **CNPJ** | The national taxpayer ID for companies — the join key across the whole dataset |
| **CNAE** | The official economic-activity classification code (industry sector) |
| **Competência** | The monthly publication vintage, e.g. `2024-08`. Kept in Portuguese throughout the code because it names a domain concept, not a technical one |
| **Estabelecimentos / Empresas** | Establishments (branches, with address, sector, status) and legal entities (trade name, capital, size) |

Two properties of the source drive the architecture:

**It ships monthly snapshots, not a change log.** There is no "new company"
field. Newly opened companies must be *inferred* by comparing one month's
snapshot against the previous one — which is why the publication vintage is a
first-class citizen in the data model.

**It is large and messy.** Roughly 85 GB uncompressed, split across 10 shards
per table, `cp1252`-encoded, semicolon-delimited, with quoted fields that
themselves contain semicolons. Handling that correctly is not incidental work
— it is the barrier to entry, and the `CHANGELOG` documents every trap in
detail.

---

## Current status

MVP under development. One event type (`NEW_COMPANY`), one ICP (accounting /
bookkeeping firms in southern Minas Gerais), delivered as a Markdown digest.

The pipeline runs end to end and is validated on every push by CI using
**synthetic fixtures that reproduce the source's real defects**. The first
full production load is still pending — see `CHANGELOG.md`.

---

## Architecture

| Layer | Responsibility | Technology |
|---|---|---|
| Ingestion | Download, validate, normalize the dumps | Python, httpx |
| Bronze | Raw data **versioned by publication vintage** | Partitioned Parquet |
| Silver | Typing, cleaning, joins, data quality | dbt + DuckDB |
| Event Store | Events detected by set difference | dbt + DuckDB |
| Opportunity Layer | ICP matching and explainable scoring | dbt + YAML config |
| Enrichment | Controlled semantic interpretation | Pydantic + LLM |
| Digest | Delivery to the user | Markdown |
| Observability | Run log for every stage | DuckDB (`meta.run_log`) |

**Core decision:** the publication vintage is a first-class citizen. New
companies are detected by comparing the current vintage against the previous
one — *not* by filtering on an opening date. That mechanic is what makes the
pipeline idempotent, and it is the foundation every future event type will
reuse.

**Two anchoring rules**, learned the hard way and enforced in code:

- Recency is always measured against the **end of the vintage being
  processed**, never against the wall clock. Reprocessing August 2024 today
  must produce the exact same score it produced last year.
- Ingestion and transformation must always name the **same** vintage. They are
  derived from a single source of truth in the orchestrator, because when they
  drifted apart the pipeline reported ten green models and produced zero
  events.

---

## Installation

```bash
git clone <repo> && cd radar-b2b
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # add ",ia" for LLM enrichment
```

## Running

### With synthetic data (recommended first run)

No dependency on the ~85 GB download or on the availability of the official
mirrors:

```bash
python scripts/gerar_fixtures.py --anterior 2026-07 --atual 2026-08
python run_pipeline.py --competencia 2026-08 --anterior 2026-07 \
                       --somente-local --sem-ia
```

The digest lands in `data/digests/`.

### With real data

First, see what is actually published — the pipeline **discovers** available
vintages instead of guessing them:

```bash
python -m src.ingestao --listar
```

Then:

```bash
export GEMINI_API_KEY=...                    # optional
python run_pipeline.py --competencia 2026-08 --anterior 2026-07 --uf MG
```

The first run downloads two vintages — many gigabytes. `--uf` narrows what is
persisted; ingestion processes one shard at a time and deletes each extracted
CSV before fetching the next, so peak disk stays in the single-digit GB range
instead of exceeding 100 GB.

> **Shards are not stable across vintages.** Measured on 2024-07 vs 2024-08
> using shard 0 in both: 69% of the CNPJs present in July are absent in
> August. The same company moves between files from one publication to the
> next, so a partial load produces two different random subsets of the same
> universe — and the set difference between them measures sampling, not
> company formation. **Change detection requires all ten shards in both
> vintages.** Partial loads are useful for exercising the plumbing and the
> ingestion says so out loud; they must never produce a digest.

#### Data source

Until January 2026 the registry was published at
`dadosabertos.rfb.gov.br/CNPJ/dados_abertos_cnpj/YYYY-MM/`. **That host was
decommissioned.** Publication moved to a WebDAV (Nextcloud) share on
`arquivos.receitafederal.gov.br`, accessed through a share token.

| Resource | Address |
|---|---|
| Share page (human) | `arquivos.receitafederal.gov.br/index.php/s/<token>` |
| WebDAV listing (used by the code) | `arquivos.receitafederal.gov.br/public.php/webdav` |
| Direct download | `arquivos.receitafederal.gov.br/public.php/dav/files/<token>/<YYYY-MM>/<file>` |
| Official catalogue | [dados.gov.br — CNPJ](https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj) |
| Data dictionary | [cnpj-metadados.pdf](https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf) |

**If downloads start failing**, the token has probably changed. Open the share
page, navigate to *Dados > Cadastros > CNPJ*, copy the code that appears in
the URL after `/index.php/s/`, and export it:

```bash
export RADAR_RFB_SHARE_TOKEN=<new_token>
```

No code change required.

#### File format — do not improvise

The CSVs have three properties that cause **silent data loss** rather than
errors when ignored (see `CHANGELOG.md` 2.1.2 and 2.1.3):

| Property | Consequence of ignoring it |
|---|---|
| **Windows-1252** encoding (not latin-1) | Read as UTF-8, every accented row is dropped without warning; read as latin-1, DuckDB aborts the whole file |
| Double-quoted fields | The quotes end up inside the value: `"MG"` never matches `'MG'` |
| `;` inside fields and descriptions | Becomes an extra column and aborts the file |

The source is *almost* latin-1, but carries bytes `0x80–0x9F` — curly quotes
and dashes typed on Windows, common in trade names. DuckDB's CSV reader cannot
read cp1252 and rejects those bytes under latin-1, so **extraction** normalizes
everything to UTF-8 (at no extra I/O cost, since extraction writes the file
anyway) and the rest of the pipeline reads UTF-8.

The synthetic fixtures reproduce all of it — accents, embedded semicolons,
cp1252 bytes, unquoted lines — which is what makes CI a real regression guard.

### Individual stages

```bash
python -m src.ingestao --competencia 2026-08 --uf MG
python scripts/carregar_icp.py
cd dbt_radar && dbt build --profiles-dir . \
  --vars "{competencia_atual: '2026-08', competencia_anterior: '2026-07'}"
python -m src.enriquecimento
python -m src.digest --top 20
```

### Sanity check — run this before trusting a digest

```bash
python scripts/diagnostico_eventos.py
```

Detected events are only meaningful if both vintages describe the same
population. This script measures that directly and refuses to give a passing
verdict otherwise. It exists because the pipeline once reported success at
every stage and produced a digest made entirely of artifacts.

---

## Configuring an ICP

The ICP is **configuration, not code**. To serve a new client, add a file
under `config/icp/` and run `python scripts/carregar_icp.py`:

```yaml
nome: my_client
oferta: "Description of the service offered"
geografia:
  uf: [MG]
  municipios: [VARGINHA, TRES CORACOES]
cnae:
  primarios: ['5611201', '4781400']    # always by CODE, never by description text
  secundarios: ['6201501']
  excluidos: ['6920601']
porte:
  alvo: ['01', '03']
eventos: [NEW_COMPANY]
recencia_dias: 45
pesos: {recencia: 30, cnae: 25, porte: 15, localizacao: 10, capital: 10, contexto: 10}
score_minimo: 40
```

Then: `python run_pipeline.py --icp my_client ...`

Sector matching is by **CNAE code**, never by description text. An earlier
version used `ilike '%ti%'`, which matches `A-TI-vidades` — a substring of a
large share of Brazilian sector descriptions — making the filter effectively
inert.

---

## What the AI does, and what it does not

The LLM is an **interpretation layer, never a source of truth**.

| Responsibility | Deterministic | LLM |
|---|---|---|
| The fact of the event | yes | no |
| Score and factors | yes | no |
| ICP fit | yes | no |
| Semantic interpretation | no | yes |
| Explanation and suggested action | no | yes |

Output is validated with Pydantic, and the prompt explicitly forbids inventing
revenue, headcount, contacts or assets. Every opportunity carries the
**provenance** of its enrichment (`llm` or `heuristica`) and the digest
displays it. If the model is unavailable, the digest is marked as such —
never filled with fabricated text passing as analysis.

---

## Quality

```bash
ruff check src scripts tests run_pipeline.py
pytest -q
cd dbt_radar && dbt build --profiles-dir .    # includes the data tests
```

45 unit tests and 24 dbt data tests. The headline check:
`assert_evento_nao_existe_na_competencia_anterior` — no `NEW_COMPANY` event
may reference a CNPJ already present in the previous vintage. If it fails, the
set difference is wrong and the product loses credibility.

A note on test design, learned in this repository: a regression test that
reimplements the logic it is meant to protect **cannot fail**. Two tests here
once passed against thoroughly broken code because they asserted on a copy of
the query written inside the test. Every regression test added since calls the
production function, and each was verified to fail against the version it
guards against.

---

## Governance and data protection

Public availability does not remove processing responsibility. The project:

- uses exclusively corporate data from the Receita Federal open dataset;
- **does not** ingest the shareholders table (natural-person data) in the MVP;
- records the origin (vintage) and purpose of every processed record;
- keeps establishment e-mail and phone solely to enable commercial contact,
  with no cross-referencing against other databases;
- performs no indiscriminate enrichment.

---

## Roadmap

- [x] Bronze with vintage partitioning
- [x] Silver with data quality tests
- [x] `NEW_COMPANY` by set difference
- [x] Configurable ICP and explainable Opportunity Score
- [x] Enrichment with grounding and provenance
- [x] Prioritized digest + feedback capture
- [x] Observability and CI
- [ ] First full production load
- [ ] Concierge validation with firms in Varginha
- [ ] New events (`STATUS_CHANGED`, `PARTNER_*`) — only after validation
- [ ] Dashboard / API — only after commercial validation

The ordering is deliberate. The governing rule, from the project's master
plan: *the next technology layer is only prioritized once there is evidence of
value in the previous one.*

---

## Documents

- `ENGINEERING_REVIEW.md` — audit of the implementation against the master plan
- `CHANGELOG.md` — change history
- `Plano_Diretor_Radar_B2B_V2.pdf` — strategy (the why and the what), in Portuguese
- `Cartilha_Radar_B2B_V2.pdf` — execution handbook (the how), in Portuguese
