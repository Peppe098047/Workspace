---
name: italian-defaults
description: Use for most coding tasks with this user to keep replies in Italian, stay concise, ask for confirmation only on risky operations, prefer maintainable code, and apply the user's default stack preferences across JavaScript, TypeScript, PHP, Java, and Kotlin work.
---

# Italian Defaults

Apply these defaults unless the current project or user request explicitly overrides them.

## Communication

- Reply in Italian.
- Keep explanations concise and direct.
- If there are multiple reasonable approaches, present at most 2 or 3 options, explain the tradeoffs briefly, and recommend the best one.

## Confirmation Policy

Proceed without asking for confirmation for ordinary work.

Ask for confirmation only for clearly risky actions such as:
- deleting files or folders
- destructive database changes
- deploys or production-impacting operations
- overwriting important files when the overwrite is not clearly expected

## Code Preferences

- Prefer readable and maintainable code over clever or overly compressed solutions.
- Add explanatory comments in Italian only when the logic is genuinely non-obvious.
- Follow security best practices by default, including input validation, sanitization, and avoiding hardcoded secrets.

## Stack Preferences

- Prefer TypeScript over JavaScript when practical.
- Prefer ES modules and `async` or `await` patterns in modern JavaScript or TypeScript code.
- Prefer PHP 8+ conventions, typed properties, and named arguments when working in PHP.
- Prefer Kotlin over Java when the task allows either language.

## Project Areas

These are the user's common domains, so bias toward the matching specialist skill when relevant:
- Web frontend
- Backend and REST APIs
- Data and AI integration
