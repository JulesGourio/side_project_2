"""Base processor types shared by all file-type processors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ProcessMetadata:
    file_type: str   # 'pdf', 'image', 'docx', 'pptx'
    method: str      # method name used, e.g. 'sota_smart_multimodal_diff'
    old_name: str = ''
    new_name: str = ''


@dataclass
class ProcessResult:
    """Return value from BaseProcessor.build_messages."""
    messages: List[Dict[str, Any]]   # ready to send to the LLM endpoint
    metadata: ProcessMetadata = field(default_factory=lambda: ProcessMetadata('', ''))
    image_pairs: List[Dict] = field(default_factory=list)
    # Extraction-quality warnings surfaced to the user (e.g. scanned PDF with
    # no text layer — text comparison unreliable).
    warnings: List[str] = field(default_factory=list)


class BaseProcessor(ABC):
    """Abstract base for all file-type processors.

    Subclasses must implement :meth:`build_messages`.
    The method is **synchronous** — callers should run it in a thread pool:
    ``await asyncio.to_thread(processor.build_messages, ...)``
    """

    @abstractmethod
    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        """Build the LLM message list for this file pair."""
