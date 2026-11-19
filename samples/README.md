# Sample PDFs

PDFs here are not committed (`samples/*.pdf` is gitignored). Download them instead:

| File | Source | Pages | Notes |
| --- | --- | --- | --- |
| `NIST.SP.800-207.pdf` | NIST SP 800-207, *Zero Trust Architecture* (2020): https://doi.org/10.6028/NIST.SP.800-207 | 59 | US government work, public domain. Has bookmarks (heading paths) and 3 tables |

Run one locally (needs `.env` and `aws login --profile ragdp`):

```bash
PYTHONPATH=services uv run python -m worker.local samples/NIST.SP.800-207.pdf \
  --tenant "$(uuidgen | tr A-Z a-z)" --doc "$(uuidgen | tr A-Z a-z)" --version 1
```
