"""
Very small in-memory store for "product drafts in progress", keyed by the
sender's WhatsApp number. This is intentionally simple (a Python dict) since
this bot is used by one person at a time. If the free Render instance
restarts (it sleeps when idle), in-progress drafts are lost -- that's fine,
just start again.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProductDraft:
    images: list[str] = field(default_factory=list)   # local file paths
    stl_path: Optional[str] = None
    stl_stats: Optional[dict] = None                   # weight_g, dims_cm, volume_cm3
    note: Optional[str] = None                          # freeform text from user
    generated: Optional[dict] = None                    # AI output (name, description, etc.)
    status: str = "collecting"                          # collecting | awaiting_confirmation | editing | confirming_delete
    edit_target_id: Optional[int] = None                 # set when editing/deleting an existing product


_drafts: dict[str, ProductDraft] = {}


def get_draft(sender: str) -> ProductDraft:
    if sender not in _drafts:
        _drafts[sender] = ProductDraft()
    return _drafts[sender]


def clear_draft(sender: str) -> None:
    _drafts.pop(sender, None)
