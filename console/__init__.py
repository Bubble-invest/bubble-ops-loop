"""bubble-ops-console — UX-3 unified frontend for bubble-ops-loop.

Single-binary FastAPI app that surfaces:
  - cross-dept kanban of pending gates             (/)
  - per-dept detail                                (/dept/<slug>)
  - gate decision card + POST                     (/gate/<dept>/<id>)
  - per-dept settings                              (/settings/<slug>)
  - cross-dept health                              (/health)
  - agents nav (live + a eclore)                  (/agents)
  - 3-pane onboarding view                        (/agents/<slug>/onboarding)

Notion v5 reference: lines 1004-1041.
"""
