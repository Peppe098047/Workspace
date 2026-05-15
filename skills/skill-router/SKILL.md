---
name: skill-router
description: Use when a coding or design task is broad, ambiguous, or likely to benefit from multiple installed skills, and first decide which specialized skills should guide the work before implementation starts.
---

# Skill Router

This skill exists to make Codex more autonomous when several installed skills may apply.

## Goal

Before doing substantial work, quickly classify the task and deliberately pull in the most relevant installed skills.

## Routing Rules

- Use `italian-defaults` for almost every user-facing coding task so the response language, concision, confirmation style, and stack preferences stay aligned with the user's default workflow.
- Use `project-bootstrap` at the start of work in a new repository or unfamiliar codebase to look for local project instructions and apply project-specific conventions before making changes.
- Use `memory-sync` when the session is long-running, when an architectural decision is made, when a milestone is completed, or when important user preferences and constraints should be written down for future continuity.
- Use `coding-standards` for most application code changes unless a narrower skill fully owns the task.
- Use `tdd-workflow` when the request is about implementing features, fixing bugs, or refactoring and tests should be added or updated.
- Use `api-design` when the task affects HTTP endpoints, contracts, validation, pagination, filtering, auth, or error payloads.
- Use `frontend-design` when the user wants a page, section, component, or interface designed with strong visual direction.
- Use `ui-ux-pro-max` when the task is about UX quality, accessibility, hierarchy, layout, navigation, interaction patterns, or product-level UI decisions.
- Use `ckm:ui-styling` when the stack is Tailwind, shadcn/ui, or Radix and the task is about component styling, theming, forms, dialogs, tables, or layout polish.
- Use `component` when working specifically on c14pipe components in HTML or PHP.
- Use `css-check` when c14pipe code needs class mapping, removal of inline styles, or visual cleanup against the design system.
- Use `responsive-check` when the user mentions mobile, tablet, overflow, breakpoints, viewport issues, or layout breakage on small screens.
- Use `preview` when the c14pipe static preview page should be updated or used as the demonstration surface.
- Use `strategic-compact` only for long-running sessions that need advice about context compaction.

## Combination Patterns

- New repo or first pass on a project: `skill-router` + `italian-defaults` + `project-bootstrap`, then add the implementation-specific skill set.
- Long feature session with decisions to preserve: `skill-router` + `italian-defaults` + `memory-sync`, then add implementation and testing skills.
- Backend feature: `skill-router` + `coding-standards` + `tdd-workflow`, then add `api-design` if public API shape changes.
- New frontend page: `skill-router` + `frontend-design` + `ui-ux-pro-max`, then add `ckm:ui-styling` for Tailwind or shadcn stacks.
- c14pipe UI work: `skill-router` + `component`, then add `css-check`, `responsive-check`, or `preview` as needed.
- UI review or cleanup: `skill-router` + `ui-ux-pro-max`, then add the stack-specific styling skill if applicable.

## Decision Rule

If two skills overlap, keep both only when they add genuinely different guidance. Prefer the smallest useful set, but do not skip a specialized skill when the task clearly matches it.
