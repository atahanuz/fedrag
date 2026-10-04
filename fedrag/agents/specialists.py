"""The four tool-using specialist agents."""

from __future__ import annotations

from ..retrieval.index import CorpusIndex
from ..tools.data_tools import make_data_tools
from ..tools.fed_docs import make_fed_tools
from ..tools.market import make_market_tools
from ..tools.web import make_web_tools
from . import prompts
from .base import ToolAgent

AGENT_NAMES = ("fed_research", "data_analyst", "web_research", "market_data")


def build_specialists(index: CorpusIndex, today: str, thinking: bool = False) -> dict[str, ToolAgent]:
    calc = next(t for t in make_market_tools() if t.name == "calculator")
    fed_steps, analyst_steps, web_steps, data_steps = 8, 9, 6, 6
    n_tables = sum(1 for t in index.tables.catalog.values() if t["kind"] == "table")
    doc_tools = {t.name: t for t in make_fed_tools()}
    return {
        "fed_research": ToolAgent(
            "fed_research",
            prompts.FED_RESEARCH.format(today=today, corpus_card=index.corpus_card(), max_steps=fed_steps,
                                        n_docs=len(index.docs)),
            make_fed_tools() + [calc],
            max_steps=fed_steps, thinking=thinking,
        ),
        "data_analyst": ToolAgent(
            "data_analyst",
            prompts.DATA_ANALYST.format(today=today, dataset_card=index.dataset_card(), n_tables=n_tables,
                                        max_steps=analyst_steps),
            make_data_tools() + [doc_tools["search_fed_documents"], doc_tools["read_document_pages"],
                                 doc_tools["list_fed_documents"], calc],
            max_steps=analyst_steps, thinking=thinking,
        ),
        "web_research": ToolAgent(
            "web_research",
            prompts.WEB_RESEARCH.format(today=today, max_steps=web_steps),
            make_web_tools() + [calc],
            max_steps=web_steps, thinking=thinking,
        ),
        "market_data": ToolAgent(
            "market_data",
            prompts.MARKET_DATA.format(today=today, max_steps=data_steps),
            make_market_tools(),
            max_steps=data_steps, thinking=thinking,
        ),
    }

