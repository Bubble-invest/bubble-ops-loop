"""bubble-git-guard — path-allow-list enforcement at the `git push` boundary.

Notion v4 line 725 (verbatim):
  "GitHub ne fournit pas un vrai path-scope au niveau token contents:write.
   Les paths autorisés sont donc appliqués par wrapper local / git guard sur
   Morty, CI path guard, branch protection et audit Layer 4. Les tokens
   limitent les repos et permissions ; les guards limitent les chemins."

This package is the LOCAL WRAPPER / GIT GUARD on Morty. The token broker
(Step 3b) covers repo + permission class; this guard covers the path
dimension.
"""

__version__ = "0.1.0"
