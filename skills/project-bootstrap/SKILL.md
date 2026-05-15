---
name: project-bootstrap
description: Use at the beginning of work in a repository or when switching projects to look for local instructions, detect project-specific conventions, and apply local guidance before making changes.
---

# Project Bootstrap

Use this skill when starting work in a project or when the repository context is not yet established.

## Primary Goal

Check whether the project has its own local instructions and make those take priority over generic defaults.

## Workflow

1. Inspect the project root for instruction files such as `CLAUDE.md`, project docs, contribution guides, or other convention files.
2. If a local instruction file exists, read it before making significant changes.
3. Treat project-local instructions as higher priority than global default preferences for that repository.
4. Then continue with the specialist skill that best matches the task, such as API, frontend, styling, testing, or code quality.

## What To Look For

- local workflow rules
- naming conventions
- stack-specific commands
- testing requirements
- code style or architecture notes
- deployment or environment constraints

## Decision Rule

If the local project instructions conflict with generic personal defaults, follow the local project instructions for that repository.
