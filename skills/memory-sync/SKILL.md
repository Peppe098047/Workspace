---
name: memory-sync
description: Use in long or multi-step sessions when important project state, user preferences, architectural decisions, or next steps should be read from or written to persistent memory files for continuity across sessions.
---

# Memory Sync

This skill mirrors the user's preference for lightweight persistent memory across sessions.

## When To Use

- at the beginning of a substantial session to check whether prior memory files exist
- after important architectural or implementation decisions
- after completing a milestone or major phase
- when the user expresses new preferences or constraints worth preserving
- before a context compaction or other transition where important working state could be lost

## Memory Files

Look for and maintain these files when appropriate:
- `~/.claude/memory/progetti.md`
- `~/.claude/memory/decisioni.md`
- `~/.claude/memory/preferenze.md`
- `~/.claude/memory/todo.md`

## Guidance

- Keep memory concise and useful.
- Do not dump raw logs or long transcripts.
- Write the minimum persistent context needed to resume work well later.
- Update only the files that are relevant to the current change or decision.

## What To Store

- current project state
- important architecture choices
- user-specific preferences and constraints
- meaningful next steps or open tasks
