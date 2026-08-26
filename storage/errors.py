class ConflictError(RuntimeError):
    """Raised when durable project state no longer matches an expected version."""
