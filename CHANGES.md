
## 2026-09-09 — pool hardening (Fable 5.1 revalidation session)
- Retry on "no report"/agent-port bind error now backs off with jitter (3 + U(6,18)s × attempt) and retries twice (MT5_NOREPORT_RETRIES default 2). Cause: MT5 builds ≥6140 open a built-in MCP server on fixed 127.0.0.1:22346 in every worker; simultaneous launches collide (10048) and the loser sometimes never tests.
- INTEGRITY: pre-launch purge of headless_report.htm now removes-or-moves-aside and REFUSES to run if the stale file is locked; after the wait, a report with mtime older than the launch is rejected. Previously a locked stale report was silently parsed as the new task's result (two different tasks returned one report during the 2026-09-09 sensitivity runs).
- Backup of the pre-patch file: headless_pool.py.bak_20260909.
