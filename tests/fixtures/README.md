# Fixtures

Synthetic samples built from the response formats documented in
[docs/research.md](../../docs/research.md) (field names from the public WB
endpoints and Avito's embedded page state as used by marketplace-mcp and
avito-mcp-plugin, both MIT; Ozon widget states, rendered tiles and JSON-LD as
described in docs/sources.md). They are **not** captured live responses: they pin
the parsing logic, not the current behaviour of the sites. Use
`sniflot probe <source> "<query>"` to check a source against the real site.
