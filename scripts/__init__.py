"""Framework script package, including helpers used outside this checkout.

An explicit package prevents an agent workspace's own ``scripts`` package from
shadowing these helpers when the floor runner prepends the framework root.
"""
