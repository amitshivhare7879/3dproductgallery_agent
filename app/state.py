"""
Store for "product drafts in progress", keyed by the sender's Telegram user
ID. Kept in memory for speed, but flushed to a JSON file on disk after every
webhook request, so an in-progress draft survives a process restart (e.g.
Render's free tier waking from sleep) instead of silently vanishing.

Note: this does NOT survive a fresh deploy (Render's disk is wiped then) --
only mid-session restarts of the same running instance. Good enough to fix
"lost my draft because the bot went quiet for a bit", not meant as a durable
database.
"""
import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

STORAGE_PATH = os.environ.get("DRAFT_STORAGE_PATH", "/tmp/bot_drafts.json")


@dataclass
class ProductDraft:
    images: list[str] = field(default_factory=list)   # local file paths
    stl_path: Optional[str] = None
    stl_stats: Optional[dict] = None                    # weight_g, dims_cm, volume_cm3
    video_path: Optional[str] = None                    # optional product video
    note: Optional[str] = None                          # freeform text from user
    generated: Optional[dict] = None                    # AI output (name, description, etc.)
    status: str = "collecting"                          # collecting | awaiting_confirmation | editing | confirming_delete
    edit_target_id: Optional[int] = None                 # set when editing/deleting an existing product


_drafts: dict[str, ProductDraft] = {}


def _load() -> None:
    global _drafts
    if not os.path.exists(STORAGE_PATH):
        return
    try:
        with open(STORAGE_PATH) as f:
            raw = json.load(f)
        _drafts = {k: ProductDraft(**v) for k, v in raw.items()}
    except Exception as e:
        # Corrupt or incompatible state file -- start fresh rather than crash.
        log.warning("Couldn't load draft storage (%s), starting fresh", e)
        _drafts = {}


def persist() -> None:
    """Best-effort flush to disk. Never let a storage failure crash the bot."""
    try:
        with open(STORAGE_PATH, "w") as f:
            json.dump({k: dataclasses.asdict(v) for k, v in _drafts.items()}, f)
    except Exception as e:
        log.warning("Couldn't persist draft storage: %s", e)


_load()


def get_draft(sender: str) -> ProductDraft:
    if sender not in _drafts:
        _drafts[sender] = ProductDraft()
    return _drafts[sender]


def clear_draft(sender: str) -> None:
    _drafts.pop(sender, None)
    persist()
