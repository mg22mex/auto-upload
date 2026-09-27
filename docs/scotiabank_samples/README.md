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
3. Tune calibrated constants in `calculator.py` / `scotiabank_profile.py` only when multiple samples agree — do not fit a single PDF.

## CrediAuto max term by model year

Enforced in `src/quote_engine/term_limits.py` (used by `CalibratedQuoteEngine` / Beatriz `/vapi/financing`):

| Model year (vs calendar year *Y*) | Max plazo |
|-----------------------------------|-----------|
| ≥ *Y* − 2 (e.g. 2024–2026 when *Y*=2026) | 60 months |
| *Y* − 3 or *Y* − 4 (e.g. 2022–2023) | 48 months |
| ≤ *Y* − 5 (e.g. 2021 or older) | 36 months |

If the requested term exceeds the cap, the quote uses the capped plazo and surfaces:
`Nota: Por el año del vehículo ({year}), el plazo máximo disponible con Scotiabank es de {max} meses.`

## Rules

- **No PII** in committed files (strip client name, phone, address, CP if required by policy).
- Do **not** commit secrets or portal session dumps.
- Live bank portals stay out of the LLM chat window; PDFs are offline calibration only.
- Related dealer corridas may also live under Autosell `Corridas/` — this folder is the repo-local, shareable subset.

## Status

Empty on purpose until samples are copied in.
