# Scotiabank sample quotes (calibration corpus)

Drop **anonymized** Scotiabank / CrediAuto PDF corridas here for French Amortization calibration against `src/quote_engine/calculator.py`.

## What to put here

| File pattern | Purpose |
|--------------|---------|
| `*.pdf` | Bank quote / tabla de amortización exports |
| Optional `*.json` sidecars | Hand-extracted fields (price, enganche, tasa, plazo, mensualidad) for regression fixtures |

## Naming

```
docs/scotiabank_samples/
  YYYYMMDD_<unidad>_<plazo>m_<enganche>.pdf
  # e.g. 20260722_cx5_24m_e300.pdf
```

## How they are used

1. Extract header fields: valor, enganche, tasa fija anual, comisión %, plazo, importe a financiar, mensualidad, seguros.
2. Diff against local `calculate_quote` / schedule (rate, fee, IVA on interest).
3. Tune calibrated constants in `calculator.py` only when multiple samples agree — do not fit a single PDF.

## Rules

- **No PII** in committed files (strip client name, phone, address, CP if required by policy).
- Do **not** commit secrets or portal session dumps.
- Live bank portals stay out of the LLM chat window; PDFs are offline calibration only.
- Related dealer corridas may also live under Autosell `Corridas/` — this folder is the repo-local, shareable subset.

## Status

Empty on purpose until samples are copied in.
