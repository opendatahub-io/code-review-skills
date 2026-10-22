"""Tests for GitLab review discussion cleanup."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

REVIEW_PATH = Path(__file__).resolve().parents[1] / "skills/gitlab-code-review/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("gitlab_review", REVIEW_PATH)
review = importlib.util.module_from_spec(SPEC)
requests_stub = ModuleType("requests")
requests_stub.Session = type("Session", (), {})
with patch.dict(sys.modules, {"requests": requests_stub}):
    SPEC.loader.exec_module(review)


def discussion(name, **note_fields):
    note = {
        "id": len(name),
        "position": {"new_path": "src/example.py", "new_line": 1},
        "body": "Finding <!-- ai-review -->",
        "resolved": False,
        "resolved_by": None,
    }
    note.update(note_fields)
    return {"id": name, "notes": [note]}


class GitLabDiscussionCleanupTests(unittest.TestCase):
    def test_keeps_resolved_and_human_replied_discussions(self):
        resolved = discussion("resolved", resolved=True)
        human_resolved = discussion(
            "human-resolved", resolved_by={"username": "reviewer", "bot": False}
        )
        human_replied = discussion("human-replied")
        human_replied["notes"].append({"id": 99, "body": "Acknowledged", "system": False})
        unresolved = discussion("unresolved")
        bot_resolved = discussion("bot-resolved", resolved_by={"bot": True})
        session = Mock()
        session.delete.return_value.status_code = 204

        with patch.object(
            review,
            "_gitlab_paginated_get",
            return_value=[resolved, human_resolved, human_replied, unresolved, bot_resolved],
        ):
            review._gitlab_delete_previous_discussions(session, "https://gitlab/api/mr")

        self.assertEqual(
            [call.args[0] for call in session.delete.call_args_list],
            [
                "https://gitlab/api/mr/discussions/unresolved/notes/10",
                "https://gitlab/api/mr/discussions/bot-resolved/notes/12",
            ],
        )


if __name__ == "__main__":
    unittest.main()
