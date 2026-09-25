import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invoice_extractor.evaluation.corpus import load_corpus  # noqa: E402
from invoice_extractor.config.settings import DEFAULT_CORPUS, Settings  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings(mode="mock", mode_reason="test", batch_poll_interval_s=0.0, batch_poll_timeout_s=5.0)


@pytest.fixture(scope="session")
def corpus():
    docs, truth = load_corpus(DEFAULT_CORPUS)
    return {d.doc_id: d for d in docs}, truth
