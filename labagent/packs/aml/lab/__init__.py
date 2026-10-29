"""Build-it-yourself notebooks for the AML RAG lab (mode=rag).

The finished tool the pack already ships (`aml_tools.AmlLab`) is the demo: it shows what good looks like. These notebooks are the lab:
the learner writes the policy search, the evidence bundle, the citation check and the triage, and scores each against the answer key.
They use the same stub/solution source format as the RAG lab (labagent/ai/rag/lab) and may reference its solved exercises.
"""
from pathlib import Path
from typing import Dict

from ....ai.rag import lab as rag_lab

HERE = Path(__file__).parent
UPSTREAM = (rag_lab.HERE,)


def sources():
    return rag_lab.sources(HERE)


def cells(path, keep):
    return rag_lab.cells(path, keep, UPSTREAM)


def student() -> Dict[str, str]:
    return rag_lab.student(HERE, UPSTREAM)


def solution() -> Dict[str, str]:
    return rag_lab.solution(HERE, UPSTREAM)
