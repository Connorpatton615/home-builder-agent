---
description: Generate a Chad-voice email follow-up checklist
argument-hint: [--days N] [--upload]
---

Run the Gmail follow-up agent. $ARGUMENTS may include `--days N` to override
the default 7-day lookback, and/or `--upload` to also save the checklist as a
Google Doc in `GENERATED TIMELINES/`.

Steps:
1. Run `hb-inbox $ARGUMENTS`.
2. Show me the per-thread classification table (which threads were flagged).
3. Show me the final checklist exactly as the agent produced it (don't summarize — Chad needs the action items verbatim).
4. Show me the cost breakdown (classifier vs writer).
5. If the upload flag was set, show the Google Doc URL.

If the agent says "Inbox zero today" — that's the win condition. Don't
add commentary; just confirm.
