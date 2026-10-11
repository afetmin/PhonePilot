---
name: phone-use
description: Execute Android messaging, search, reading, forms, or multi-step phone tasks through Phone Use Runner's task-level MCP. Plan once, let Jev handle UI decisions, and automatically resume context handoffs.
---

Read `../../src/phone_use_runner/agent.md` for the task and handoff workflow.

Use `phone_devices`, `phone_run`, `phone_wait`, `phone_resume`, and `phone_cancel` from the configured MCP server. Keep planning and summary in the calling agent. Use the tool schemas for concrete fields and completion conditions.

When setup is missing, follow `../../README.md` to install this project's Phone Bridge APK, configure Jev, and perform the one-time verified pairing. After setup, automatically handle ordinary task handoffs without per-action human confirmation.
