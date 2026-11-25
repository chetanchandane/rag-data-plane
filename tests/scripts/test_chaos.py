"""scripts/chaos.py's synthetic PDF: the crash row compares a crashed run's chunks with a
clean run's by hash, which only works if the same --pages gives the same bytes."""

import sys

import pymupdf

from tests.leak.matrix import load_script
from worker.chunking import chunk
from worker.pdf import parse

# chaos.py does `import issuer`, which works when it runs from scripts/. Registered only while
# chaos.py loads, so nothing else in the session picks up this copy.
sys.modules["issuer"] = load_script("issuer")
try:
    chaos = load_script("chaos")
finally:
    del sys.modules["issuer"]


def test_the_synthetic_pdf_is_deterministic():
    assert chaos.make_pdf(2) == chaos.make_pdf(2)
    assert pymupdf.open(stream=chaos.make_pdf(3)).page_count == 3


def test_the_synthetic_pdf_parses_into_distinct_chunks():
    chunks = chunk(parse(chaos.make_pdf(2)))
    assert len(chunks) >= 2
    assert len({c.content for c in chunks}) == len(chunks)
