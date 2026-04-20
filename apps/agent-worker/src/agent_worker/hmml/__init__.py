"""HMML (Hierarchical Math Modeling Library) knowledge base.

Seeds 30+ canonical math modeling methods and provides BM25 retrieval so the
Modeler agent can ground its approach choice in a reference library.
"""

from agent_worker.hmml.service import HMMLService

__all__ = ["HMMLService"]
