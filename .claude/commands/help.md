---
description: Ask the Help Desk agent a question about the AI system
argument-hint: <your question>
---

Ask the Help Desk agent anything about how the system works.

Run: `hb-help $ARGUMENTS`

The agent knows every slash command, all background automation behavior, and
common troubleshooting steps. It answers in plain English (Chad-accessible)
with enough technical detail for operator debugging.

Good questions to ask:
- "How do I generate a timeline for a new project?"
- "The inbox watcher isn't notifying me — what do I do?"
- "What does the Dashboard tab actually show?"
- "How do I reload the watcher after editing its code?"
- "What does re-running /timeline do to the old documents?"

After answering, show:
1. The full answer exactly as the agent produced it
2. Whether the Q&A was added to the FAQ doc (and what question was saved)
3. The cost

If the agent says it added something to the FAQ, that's the win condition —
the system just got smarter. Don't add commentary.

Pass --no-faq to skip the FAQ update if $ARGUMENTS ends with that flag.
