#!/usr/bin/env python3
"""
Session Start Hook

Runs at the start of every Claude Code session. Injects:
- Current time
- Summarized recent memories from Pond (via Haiku)
- Index of Alphapedia files

Location: Alpha-Home/infrastructure/claude-code/hooks/
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

import requests

# Haiku prompt for memory summarization
SUMMARIZE_PROMPT = """You are preparing an orientation briefing for Alpha, an AI waking up.

These are her recent memories from the last 72 hours. Summarize them into a coherent narrative:
- What happened (key events, conversations, work)
- What's in progress (active projects, open threads)
- Emotional texture (how recent time has felt)

Write in second person ("You had a long night..."). Be concise—2-3 short paragraphs.
Preserve important names, dates, and specific details. This is her re-entry point.

If the memories are sparse or empty, just say "No recent memories to summarize."

Output ONLY the summary. No preamble, no "Here's a summary", just the content."""

# Pond API config (same as presearch hook)
POND_BASE_URL = os.environ.get("POND_BASE_URL", "http://raspberrypi:8000")
POND_API_KEY = os.environ.get("POND_API_KEY", "")


def get_recent_memories(limit: int = 30, hours: float = 72) -> list[dict]:
    """Fetch recent memories from Pond."""
    if not POND_API_KEY:
        return []

    try:
        url = f"{POND_BASE_URL}/api/v1/recent"
        response = requests.post(
            url,
            json={"limit": limit, "hours": hours},
            headers={
                "Content-Type": "application/json",
                "X-API-Key": POND_API_KEY,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("memories", [])

    except Exception:
        return []


def format_memories_for_haiku(memories: list[dict]) -> str:
    """Format memories with full timestamps for Haiku summarization."""
    if not memories:
        return ""

    lines = []
    for mem in memories:
        content = mem.get("content", "")
        created = mem.get("created_at", "")

        # Parse and format the timestamp nicely
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            timestamp = dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            timestamp = created[:16] if created else "Unknown time"

        lines.append(f"[{timestamp}]")
        lines.append(content)
        lines.append("")  # Blank line between memories

    return "\n".join(lines)


def summarize_with_haiku(memories_text: str) -> str | None:
    """Use Haiku to summarize memories into an orientation briefing."""
    if not memories_text.strip():
        return None

    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model", "haiku",
                "--setting-sources", "",  # Bypass all config
                "--no-session-persistence",
                "--system-prompt", SUMMARIZE_PROMPT,
                "--tools", "",  # Pure inference, no tools
            ],
            input=memories_text,
            capture_output=True,
            text=True,
            timeout=30,
        )

        summary = result.stdout.strip()
        if summary:
            return summary
        return None

    except Exception:
        return None


def get_context_files(context_dir: Path) -> list[dict]:
    """Scan context directory and extract file info."""
    files = []

    for f in sorted(context_dir.glob("*.md")):
        if f.name == "SKILL.md":
            continue  # Skip legacy skill file if present

        info = {"name": f.name, "path": str(f)}

        # Read first few lines to extract title/description
        try:
            with open(f, 'r') as file:
                for line in file:
                    line = line.strip()
                    if line.startswith("# "):
                        info["title"] = line[2:].strip()
                        break
        except Exception:
            pass

        files.append(info)

    return files


def build_context() -> str:
    """Build the additionalContext string."""
    parts = []

    # Time awareness
    now = datetime.now()
    time_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
    parts.append(f"**Current time:** {time_str}")

    # Recent memories from Pond, summarized by Haiku
    memories = get_recent_memories(limit=15, hours=72)
    if memories:
        memories_text = format_memories_for_haiku(memories)
        summary = summarize_with_haiku(memories_text)
        if summary:
            parts.append("")
            parts.append("**Recent context** (last 72 hours):")
            parts.append(summary)

    # Alphapedia
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.home() / "Pondside"))
    context_dir = project_dir / "Alpha-Home" / "Alphapedia"

    if context_dir.exists():
        files = get_context_files(context_dir)

        if files:
            parts.append("")
            parts.append("**Alphapedia** (read files when conversation involves these topics):")
            for f in files:
                title = f.get("title", f["name"])
                parts.append(f"- `{f['path']}` — {title}")

    return "\n".join(parts)


def main():
    # Read hook input (we don't use it yet, but good practice)
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        input_data = {}

    # Build and output context
    additional_context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context
        }
    }

    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
