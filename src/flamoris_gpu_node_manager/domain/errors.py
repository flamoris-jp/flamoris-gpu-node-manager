"""Errors exposed by the runtime authority."""


class NodeManagerError(Exception):
    """Base error for expected manager failures."""


class ProfileValidationError(NodeManagerError):
    """A runtime profile is malformed or unsafe."""


class UnknownRuntimeError(NodeManagerError):
    """The requested runtime is not in the validated registry."""


class RuntimeUnavailableError(NodeManagerError):
    """The requested runtime is configured but unavailable."""


class TransitionError(NodeManagerError):
    """A runtime transition failed at a bounded step."""


class TransitionBusyError(NodeManagerError):
    """Another runtime transition owns the global lock."""
