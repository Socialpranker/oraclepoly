"""Auto Edge Scanner package — weekly hypothesis discovery on canonical features.

Mirrors the manual D11→D14 research workflow (hypothesis → grid sweep → strict
gate → mirror check → year split → capacity → dedup → verdict) but runs
unattended, persists results to Postgres, and emits Telegram alerts only on
novel passing candidates.

Plan: ~/.claude/plans/wiggly-nibbling-stardust.md
"""
