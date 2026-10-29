"""Per-stage resume logic. Already-processed IDs are never sent to the LLM again (Guardrails rule 4)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, List, Set


class Checkpoint:
    def __init__(self, stage: str, directory: Path):
        self.path = Path(directory) / f"{stage}.json"
        self._done: Set[str] = set()
        if self.path.exists():
            self._done = set(json.loads(self.path.read_text(encoding="utf-8")).get("done_ids", []))

    @property
    def done_count(self) -> int:
        return len(self._done)

    def is_done(self, item_id: str) -> bool:
        return item_id in self._done

    def pending(self, ids: Iterable[str]) -> List[str]:
        return [i for i in ids if i not in self._done]

    def unmark(self, ids: Iterable[str]) -> None:
        """Forget ids so they are processed again (used only by an explicit --redo-test)."""
        self._done.difference_update(ids)
        self._save()

    def mark_done(self, ids: Iterable[str]) -> None:
        self._done.update(ids)
        self._save()

    def _save(self) -> None:
        # Atomic write: a crash mid-write can never corrupt the checkpoint.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"done_ids": sorted(self._done)}), encoding="utf-8")
        os.replace(tmp, self.path)
