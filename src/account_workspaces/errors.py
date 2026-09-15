class WorkspaceError(Exception):
    """An actionable, safe-to-display error (never include resolved secrets)."""
