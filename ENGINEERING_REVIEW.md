**English** · [Português](ENGINEERING_REVIEW.pt-BR.md)

# Engineering review against the master plan (V2)

Date: 14/09/2026 · Reference: `Plano_Diretor_Radar_B2B_V2.pdf` and
`Cartilha_Radar_B2B_V2.pdf` (both in Portuguese)

An audit of how faithfully the code implements the master plan, produced by
reading both documents in full and every module in the project. The goal is to
reach **empirical validation** (§14 of the plan) knowing what is ready, what is
broken, and what is noise.

> Items marked **RESOLVED in 2.2.0** were fixed after this review was written.
> They are kept on the record because the findings explain how the defects
> survived for so long, which is more useful than a clean page.

---

## 1. Verdict

The architecture is **faithful to the master plan** — in several places
literally so. The Opportunity Score weights in `contabilidade_sul_mg.yaml`
(recency 30, CNAE 25, size 15, location 10, capital 10, context 10) reproduce
the §10 table exactly. The Pydantic contract for enrichment implements §11
rigorously, including the explicit ban on inventing revenue, shareholders or
contacts. `meta.run_log` covers §16. Not ingesting the shareholders table
satisfies §17.

What separates the project from real use **is not architecture, it is data**:
roadmap Phase 4 ("NEW_COMPANY — new companies detected without duplication")
has not yet met its Definition of Done.

This review also uncovered a severe defect: **`run_pipeline.py` no longer
accepted command-line arguments**, even though the README and both GitHub
Actions workflows depend on it.

---

## 2. Adherence by roadmap phase (§15)

| Phase | Definition of Done | Status |
|---|---|---|
| 0 · Diagnosis | State and priorities defined | ✅ |
| 1 · Setup | Pipeline runs locally | ✅ |
| 2 · Bronze | Vintage downloaded, validated, persisted | ⚠️ validation OK; full load pending |
| 3 · Silver | Typed tables and passing tests | ✅ 24 dbt tests |
| 4 · NEW_COMPANY | New companies **without duplication** | ❌ **current blocker** |
| 5 · ICP | Events filtered by ICP | ✅ |
| 6 · Score | Score and factors per opportunity | ✅ |
| 7 · LLM | Structured JSON + confidence + grounding | ⚠️ grounding fields not recorded |
| 8 · Digest | User receives and understands | ✅ |
| 9 · Validation | Feedback and action signals collected | ⛔ not started (depends on Phase 4) |
| 10 · Automation | Pipeline schedules and logs runs | ✅ *(2.2.0 — workflow existed but was inert)* |
| 11 · Expansion | Only events with proven value | ⛔ correctly deferred |
| 12 · Platform | After commercial validation | ⛔ correctly deferred |

---

## 3. Findings

### 3.1 CRITICAL — the orchestrator lost its command-line interface

> **RESOLVED in 2.2.0.** Kept on the record because the finding explains why
> CI spent months never exercising the fixtures.

`run_pipeline.py` hardcoded vintages, shards and state in constants and **did
not read `sys.argv`**. Meanwhile:

- the **README** documented
  `python run_pipeline.py --competencia 2026-08 --anterior 2026-07 --somente-local --sem-ia`;
- **`ci.yml`** called exactly that line;
- **`pipeline-mensal.yml`** called `--uf MG --competencia <month>`.

Python does not complain about arguments nobody reads: they were **silently
ignored**. Verified by running `main()` with the exact `ci.yml` line and
intercepting the commands:

```
input:    --competencia 2026-08 --anterior 2026-07 --somente-local --sem-ia

actually executed:
  python -m src.ingestao --competencia 2024-07 --fatias 0,1,...,9 --uf MG
  python -m src.ingestao --competencia 2024-08 --fatias 0,1,...,9 --uf MG
  ...
  honoured --somente-local?  False
  honoured --sem-ia?         False
```

The consequences:

- **CI did not test what it claimed to test — and worse.** It generated
  fixtures, asked for `--somente-local`, and the pipeline went off to download
  all ten real shards. In other words, **every push to `main` triggered an
  attempt to download tens of gigabytes** from a historically unstable host.
  The job failed on time or network, never for the reason anyone would guess,
  and the fixtures — the only real guard against parsing regressions — were
  never exercised.
- **Monthly automation was inert.** The `--competencia` input from
  `workflow_dispatch` was discarded; every 5th of the month the pipeline would
  reprocess 2024-08 regardless of what had been published.

The fix was `argparse` defaulting to the existing constants — preserving
current behaviour while restoring the documented interface.

### 3.2 CRITICAL — Phase 4 without its DoD: shards are not stable

Documented in CHANGELOG 2.1.7. Measurement: with the same shard 0 in both
vintages, **69.2% of the CNPJs present in July are absent in August**. The
same company moves between files on each publication, so the set difference
between partial loads measures sampling, not company formation.

There is no shortcut: detection requires all ten shards in both vintages.
Shard-by-shard ingestion (2.1.7) made that feasible on disk; the load remains
to be run.

### 3.3 HIGH — silently ignored ICP keys

> **RESOLVED in 2.2.0.**

`contabilidade_sul_mg.yaml` declared:

```yaml
geografia:
  uf: [MG]           # <- never read
  municipios: [...]  # <- read
eventos: [NEW_COMPANY]   # <- never read
```

`scripts/carregar_icp.py` read only `geografia.municipios`, `cnae`, `porte`
and `pesos`. Anyone configuring a new ICP would assume `uf` and `eventos`
worked — §9 of the plan lists both as ICP attributes. Restricting by state in
the YAML had no effect whatsoever, and the day a second event type exists,
`eventos:` would have remained decorative.

Same family as the bugs found throughout this session: configuration that
looks active and is not.

### 3.4 MEDIUM — gaps in observability (§16)

`meta.run_log` is sound, but only three stages register in it: ingestion,
enrichment and digest. **dbt and the ICP load do not appear**, because the
orchestrator runs them as subprocesses without wrapping them in `obs.etapa`.

§16 explicitly asks to record:

- *"the number of events detected and opportunities generated"* — today
  `run_log` stores ingestion `registros`, not the event or opportunity counts;
- *"the number **and cost** of LLM calls"* — `chamadas_llm` exists, cost does
  not.

### 3.5 MEDIUM — grounding without a record of the fields used (§11)

§11 asks that inferences carry *"a reference to the fields used for grounding,
where technically feasible"*. `ContextoComercial` has `confianca` but does not
record which fields the interpretation came from. Since the prompt is built
from a fixed, known set of columns, this is perfectly feasible — and it is
what allows an explanation to be audited months later.

### 3.6 OPTIMIZATION — `empresas` ingested for the whole country

> **RESOLVED in 2.2.0.**

`Estabelecimentos` is filtered by state; `empresas` was not, because the table
has no state column. With ten shards, Bronze would hold **~180 million rows
per vintage** to serve ~2 million establishments in Minas Gerais.

Since `empresas` is only used in a join by `cnpj_basico` against
already-filtered establishments, it can be restricted to the surviving set — a
two-order-of-magnitude reduction in disk and in `slv_empresas` runtime.

### 3.7 LOW — orphaned `python-package-conda.yml`

`ci.yml` was created to replace GitHub's default template, but the template
remains in the repository and runs on every push, failing. Noise in the CI
history.

---

## 4. What is right and should not be touched

Recorded explicitly, because reviews tend to discuss only what is wrong:

- **Deterministic, auditable score** (§10): six factors, weights from the ICP,
  `score_fatores` as JSON and the explanation derived from the factors rather
  than hand-concatenated. Meets the plan's transparency requirement.
- **ICP as configuration** (§9): adding a client means creating a YAML file,
  not editing SQL.
- **The AI's boundary** (§11): the LLM decides neither fact nor score, output
  is validated with Pydantic, provenance (`llm` | `heuristica`) is persisted,
  and the digest marks itself as un-enriched when the model is unavailable.
  Exactly the discipline the plan demands.
- **Feedback loop** (§13): `gold.opportunity_feedback` already records what
  was sent, ready to receive the firm's response.
- **Data protection** (§17): shareholders out of scope, vintage recorded as
  origin, contact details kept only for the establishment.
- **Fixtures that reproduce the source's defects**: accents, `;` inside
  fields, cp1252 curly quotes, unquoted lines. That is what turns CI into a
  real guard — provided it runs at all (3.1).

---

## 5. Suggested priorities

| # | Item | Why now |
|---|---|---|
| ~~1~~ | ~~Restore the `run_pipeline.py` CLI~~ (3.1) | ✅ done in 2.2.0 |
| 2 | Full ten-shard load (3.2) | Phase 4's DoD and the gate for everything downstream |
| ~~3~~ | ~~Read `geografia.uf` and `eventos` from the ICP~~ (3.3) | ✅ done in 2.2.0 |
| 4 | Events/opportunities and LLM cost in `run_log` (3.4) | §16; and without it validation cannot be measured |
| ~~5~~ | ~~Filter `empresas` by state~~ (3.6) | ✅ done in 2.2.0 |
| 6 | Grounding fields (3.5) | §11; improves auditability, does not block validation |
| 7 | Remove the orphaned workflow (3.7) | Housekeeping |

Items 1, 3 and 5 were small and had to land **before** the full load: item 5
changes what the load writes, and redoing 180 million rows afterwards would be
pure waste.

---

## 6. Step by step to empirical use

Each stage has a **verifiable acceptance criterion**. If the criterion does
not hold, the problem is there — do not advance carrying doubt.

### ~~Stage 1 · Unblock the interface~~ ✅ completed in 2.2.0

Three small fixes that had to precede the full load, because two of them
change what the load writes: the `run_pipeline.py` CLI (3.1), the ICP honouring
`geografia.uf` and `eventos` (3.3), and `empresas` filtered by the state's
`cnpj_basico` set (3.6).

*Accepted:* `pytest -q` green (45 tests); the CI line runs on fixtures without
touching the network.

### Stage 2 · Full load (hours, interruption-tolerant)

```powershell
python run_pipeline.py
```

All ten shards of both vintages. `[SKIP]` reuses whatever is already on disk,
so drops are resumable — rerun as many times as needed. Prefer off-peak hours;
the tax authority's infrastructure has been expensive in this project.

*Accepted:* both vintages with ten Parquet files each under
`data/bronze/estabelecimentos/competencia=*/`, and `dbt build` closing at
`PASS=34 ERROR=0`.

### Stage 3 · The quality gate

```powershell
python scripts/diagnostico_eventos.py
```

This is the moment that decides whether the product exists. Expected figures
now that both months describe the same population:

| Indicator | Expected | If it differs |
|---|---|---|
| `[2.1]` disappearances | near 0% | populations still diverge; do not advance |
| events detected | thousands, not hundreds of thousands | registration-status filter or wrong vintage |
| dates inside the window | the majority | ingestion artifacts remain |
| verdict | "eventos coerentes" | investigate before looking at the digest |

*Accepted:* positive verdict. **Without it, nothing downstream means
anything.**

### Stage 4 · Read the digest as a product, not as script output

Here the nature of the work changes: engineering up to this point, product
from here on. Open `data/digests/` and read it as the accountant in Varginha
would:

- Is the volume usable? §14 of the plan asks for **10 to 30 opportunities** —
  if 300 arrive, raise `score_minimo`; if 3 arrive, the ICP is too narrow.
- Does the "why it matters" explanation convince, or is it generic?
- Is the suggested action concrete enough for someone to execute today?
- Are the ICP's CNAEs really the clients an accounting firm wants? The current
  list (restaurants, apparel, construction, medical, freight) is your
  hypothesis, not data — and it is the first thing a real firm will challenge.

*Accepted:* you would look at this list and know who to contact first.

### Stage 5 · AI enrichment

Only now is it worth spending quota: semantically interpreting opportunities
derived from bad data is expensive and useless. Configure the key through the
environment, never in code:

```powershell
setx GEMINI_API_KEY "your-key"
```

*Accepted:* digest without the "NOT ENRICHED" marker, and explanations passing
the §11 test — none citing revenue, headcount, shareholders or contacts absent
from the data.

### Stage 6 · Concierge (§14) — actual empirical use

The plan is specific and worth following to the letter:

1. One or two firms in Varginha. Two beats one: disagreement between them
   teaches more than agreement from a single source.
2. One ICP per firm — a separate YAML file, which the design already supports.
3. Deliver the digest **by hand**. No send automation yet; the value of this
   phase is in watching the reaction.
4. The right question, per §1.4 of the plan, is **not** *"do you want this
   signal?"* — the market has already proven that. It is: *"what does this
   give you that the alternative does not, and would you pay for it?"*
5. Record the response in `gold.opportunity_feedback`, which is ready for it.

*Accepted:* the §13 metrics leaving zero — % considered useful, % contacted,
replies.

### What NOT to do yet

The plan states an explicit advancement rule: *"the next technology layer is
only prioritized once there is evidence of value in the previous one."* So,
until Stage 6 produces a signal:

- no new event types (`STATUS_CHANGED`, `PARTNER_*`);
- no dashboard or API;
- no delivery automation.
