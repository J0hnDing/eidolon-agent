# Hugging Face Papers Integration

Eidolon exposes the public Hugging Face Papers catalog as three trusted read-only integration functions. The provider does not require a token or a Settings connection.

## Operations

### `huggingface.list_papers`

Lists Hugging Face Daily Papers for one period in the source ranking order.

```json
{
  "period": "2026-09",
  "sort": "trending",
  "limit": 15
}
```

`period` accepts an ISO month (`YYYY-MM`), ISO week (`YYYY-Www`), or date (`YYYY-MM-DD`). `sort` accepts `trending` or `publishedAt` and defaults to `trending`. `limit` defaults to 15 and is bounded to 100.

### `huggingface.search_papers`

Searches indexed paper titles, authors, abstracts, and content.

```json
{
  "query": "world models",
  "limit": 15
}
```

The query is limited to 250 characters. `limit` defaults to 15 and is bounded to 120.

Both list and search return `PaperSummary[]` directly. Each summary contains:

- `paper_id`: stable arXiv identifier without a version suffix
- `title`
- `authors`: author names as strings
- `abstract`
- `url`: stable Hugging Face paper page
- `pdf_url`: stable arXiv PDF URL
- `published_at`: provider timestamp or `null`
- `upvotes`: Hugging Face upvotes

The function and web-app integration helpers preserve the raw array. MCP requires object-shaped structured content, so Eidolon projects an array operation as `{"result": [...]}` at that boundary and publishes the correspondingly wrapped MCP output schema.

### `huggingface.get_paper`

Reads one paper and can include bounded full-paper text.

```json
{
  "paper_id": "2602.08025",
  "include_content": true
}
```

The result includes all `PaperSummary` fields plus:

- `content`: full-paper text or `null` when not requested
- `content_source`: `arxiv_html`, `arxiv_pdf`, or `null`
- `content_truncated`: whether Eidolon clipped extracted text at 500,000 characters

When content is requested, the provider reads the actual arXiv HTML paper first. If HTML is unavailable or unusable, it downloads the bounded arXiv PDF and extracts its text. A Hugging Face abstract or ordinary paper-page fallback is never labeled as full-paper content. The PDF download is limited to 20 MB, HTML to 8 MB, and extracted text to 500,000 characters.

Content retrieval can require the metadata request followed by HTML and PDF fallback. Services should use an integration helper timeout above the 30-second default, such as 100 seconds, while remaining within their overall run timeout.

## Provider boundary

The adapter calls only these HTTPS endpoints:

- `GET https://huggingface.co/api/daily_papers`
- `GET https://huggingface.co/api/papers/search`
- `GET https://huggingface.co/api/papers/{paper_id}`
- `GET https://arxiv.org/html/{paper_id}`
- `GET https://arxiv.org/pdf/{paper_id}`

Redirects are rejected. Response sizes, result counts, IDs, periods, and normalized output schemas are enforced before data reaches a caller. Provider failures are reduced to normalized errors such as `not_found`, `rate_limited`, `provider_timeout`, `response_too_large`, `unsupported_file_type`, and `provider_unavailable`.
