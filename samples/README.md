# Sample PDFs

PDFs here are not committed (`samples/*.pdf` is gitignored). Download them instead:

| File | Source | Pages | Notes |
| --- | --- | --- | --- |
| `NIST.SP.800-207.pdf` | NIST SP 800-207, *Zero Trust Architecture* (2020): https://doi.org/10.6028/NIST.SP.800-207 | 59 | US government work, public domain. Has bookmarks (heading paths) and 3 tables |
| `NIST.SP.800-207-p1-20.pdf` | Its first 20 pages, for the Slice 1 end-to-end run (`docs/runbooks/slice1-e2e.md`) | 20 | Made from the file above, command below |

```bash
uv run python -c "import pymupdf; d = pymupdf.open('samples/NIST.SP.800-207.pdf'); \
d.select(range(20)); d.save('samples/NIST.SP.800-207-p1-20.pdf', garbage=3, deflate=True)"
```

Run one locally (needs `.env` and `aws login --profile ragdp`):

```bash
PYTHONPATH=services uv run python -m worker.local samples/NIST.SP.800-207.pdf \
  --tenant "$(uuidgen | tr A-Z a-z)" --doc "$(uuidgen | tr A-Z a-z)" --version 1
```
