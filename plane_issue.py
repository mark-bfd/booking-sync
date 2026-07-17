"""
Plane issue helper — solves the sequence_id collision bug.

Plane's self-hosted API doesn't auto-increment from max(seq)+1; it picks from
gaps. This wrapper explicitly sets sequence_id = max+1 at creation time, then
verifies post-create and patches if Plane ignored the hint.

Configuration (all via environment variables):

    PLANE_API_KEY       API key for your Plane instance (required)
    PLANE_HOST          e.g. http://localhost:8083 (or your server's address)
    PLANE_WORKSPACE     workspace slug, e.g. your-workspace
    PLANE_PROJECT_ID    project UUID
    PLANE_ISSUE_PREFIX  display prefix for issue numbers, e.g. PROJ

State IDs are per-project in Plane — fetch yours once with
GET /api/v1/workspaces/<ws>/projects/<id>/states/ and export them:

    PLANE_STATE_BACKLOG, PLANE_STATE_TODO, PLANE_STATE_INPROGRESS,
    PLANE_STATE_DONE, PLANE_STATE_CANCELLED

Usage as a library:

    from plane_issue import PlaneClient
    p = PlaneClient()
    issue = p.create_issue({
        "name": "...",
        "priority": "high",
        "description_html": "...",
        "target_date": "2026-05-15",
    })
    print(issue["sequence_id"])  # guaranteed unique

Usage as a CLI (for one-off creates):

    python plane_issue.py create --name "..." --priority high --target 2026-05-15

Notes:
- Uses urllib.request to keep zero-deps.
- Handles both POST-time sequence_id support and PATCH fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

DEFAULT_HOST = os.environ.get("PLANE_HOST", "http://localhost:8083")
DEFAULT_WORKSPACE = os.environ.get("PLANE_WORKSPACE", "your-workspace")
DEFAULT_PROJECT = os.environ.get("PLANE_PROJECT_ID", "")
ISSUE_PREFIX = os.environ.get("PLANE_ISSUE_PREFIX", "PROJ")

# Per-project state UUIDs — fetch from your own Plane instance (see module docstring).
STATE_BACKLOG = os.environ.get("PLANE_STATE_BACKLOG", "")
STATE_TODO = os.environ.get("PLANE_STATE_TODO", "")
STATE_INPROGRESS = os.environ.get("PLANE_STATE_INPROGRESS", "")
STATE_DONE = os.environ.get("PLANE_STATE_DONE", "")
STATE_CANCELLED = os.environ.get("PLANE_STATE_CANCELLED", "")


class PlaneClient:
    def __init__(
        self,
        api_key: str | None = None,
        host: str = DEFAULT_HOST,
        workspace: str = DEFAULT_WORKSPACE,
        project: str = DEFAULT_PROJECT,
    ) -> None:
        # Resolution order: explicit arg > PLANE_API_KEY env var.
        self.api_key = api_key or os.environ.get("PLANE_API_KEY")
        if not self.api_key:
            raise RuntimeError("PLANE_API_KEY is not set (env var or explicit arg).")
        if not project:
            raise RuntimeError("PLANE_PROJECT_ID is not set (env var or explicit arg).")
        self.host = host
        self.workspace = workspace
        self.project = project
        self.base = f"{host}/api/v1/workspaces/{workspace}/projects/{project}"

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-API-Key": self.api_key,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} -> {e.code}: {err_body}") from e

    def list_issues(self, per_page: int = 200) -> list[dict]:
        d = self._request("GET", f"/issues/?per_page={per_page}")
        return d.get("results", []) if isinstance(d, dict) else d

    def get_issue(self, issue_id: str) -> dict:
        return self._request("GET", f"/issues/{issue_id}/")

    def patch_issue(self, issue_id: str, fields: dict) -> dict:
        return self._request("PATCH", f"/issues/{issue_id}/", fields)

    def add_comment(self, issue_id: str, html: str) -> dict:
        return self._request("POST", f"/issues/{issue_id}/comments/", {"comment_html": html})

    def next_sequence_id(self) -> int:
        issues = self.list_issues()
        if not issues:
            return 1
        return max(it["sequence_id"] for it in issues) + 1

    def create_issue(self, payload: dict, *, force_unique_seq: bool = True) -> dict:
        """Create an issue with collision-safe sequence_id assignment.

        Strategy:
        1. Read current max sequence_id, set payload["sequence_id"] = max+1
        2. POST. Plane may honor the hint, or assign something else (collides).
        3. If returned sequence_id collides with another existing issue,
           PATCH to set it to a fresh max+1.
        """
        if not force_unique_seq:
            return self._request("POST", "/issues/", payload)

        target_seq = self.next_sequence_id()
        payload_with_seq = {**payload, "sequence_id": target_seq}
        try:
            created = self._request("POST", "/issues/", payload_with_seq)
        except RuntimeError:
            # Plane API may reject sequence_id at POST. Fall back: create without, then patch.
            created = self._request("POST", "/issues/", payload)

        assigned = created.get("sequence_id")
        if assigned == target_seq:
            return created  # success on first try

        # Verify whether the assigned seq collides
        issues = self.list_issues()
        seq_count = defaultdict(int)
        for it in issues:
            seq_count[it["sequence_id"]] += 1

        if seq_count[assigned] <= 1:
            return created  # Plane assigned a different but unique seq, fine

        # Collision detected - patch to next free
        new_seq = max(seq_count) + 1
        patched = self.patch_issue(created["id"], {"sequence_id": new_seq})
        return patched


def _cli() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="Create a new issue")
    create.add_argument("--name", required=True)
    create.add_argument("--description-html", default="")
    create.add_argument("--priority", default="medium",
                        choices=["urgent", "high", "medium", "low", "none"])
    create.add_argument("--target", default=None, help="YYYY-MM-DD target_date")
    create.add_argument("--state", default="todo",
                        choices=["backlog", "todo", "in_progress", "done", "cancelled"])
    create.add_argument("--parent", default=None, help="Parent issue UUID")

    sub.add_parser("next-seq", help="Print next free sequence_id")

    args = p.parse_args()
    client = PlaneClient()

    if args.cmd == "next-seq":
        print(client.next_sequence_id())
        return 0

    if args.cmd == "create":
        state_map = {
            "backlog": STATE_BACKLOG,
            "todo": STATE_TODO,
            "in_progress": STATE_INPROGRESS,
            "done": STATE_DONE,
            "cancelled": STATE_CANCELLED,
        }
        payload = {
            "name": args.name,
            "priority": args.priority,
            "state": state_map[args.state],
        }
        if args.description_html:
            payload["description_html"] = args.description_html
        if args.target:
            payload["target_date"] = args.target
        if args.parent:
            payload["parent"] = args.parent
        issue = client.create_issue(payload)
        print(f"{ISSUE_PREFIX}-{issue['sequence_id']}  id={issue['id']}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli())
