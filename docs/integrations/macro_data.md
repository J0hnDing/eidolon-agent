# Macro data source integrations

Eidolon exposes four bounded read operations for the Macro / Geopolitical News Scout:

- `fred.release.list` reads recent FRED release dates and requires a FRED API key.
- `bls.series.latest` reads a small set of current BLS CPI, unemployment, and payroll observations. BLS's public endpoint works without a key; an optional BLS key is accepted for higher quotas.
- `bea.series.latest` reads selected recent BEA NIPA observations and requires a BEA API key.
- `eia.series.latest` reads current WTI and Henry Hub observations through the EIA API v2 and requires an EIA API key.

Each operation accepts an optional ISO `since` value and a `limit` from 1 through 25. The normalized response is bounded to:

```json
{
  "items": [
    {
      "id": "stable source identifier",
      "title": "source title",
      "url": "https://official-source.example/item",
      "published_at": "2026-09-23T00:00:00Z",
      "summary": "short source summary",
      "source": "FRED"
    }
  ]
}
```

FRED release records use the source release date. BLS, BEA, and EIA series APIs expose observation periods rather than publication timestamps, so those records return `published_at: null` and include the observation period in the title or summary. The scout treats these as bounded snapshots and uses official release or news feeds for publication timing.

The Integrations settings page provides write-only FRED, BLS, BEA, and EIA API-key fields. Keys are stored under the OS credential manager namespaces `fred`, `bls`, `bea`, and `eia`. A saved key is shown as configured; saving does not verify it, and a later source call may reject it. BLS remains available without a key. Status responses contain only configuration and availability metadata; API keys, secret-store references, and provider request credentials never enter skill inputs, outputs, audit records, prompts, or report sources. The backend uses fixed official host allowlists and never puts an API key in a returned source URL.
