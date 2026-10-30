"""Exception hierarchy.

The split that matters operationally is retryable or not: only
``TransientDatabaseError`` is retried. Retrying anything else repeats the same
failure, so it fails the run immediately with its reason recorded.
"""


class DataFlowError(Exception):
    """Base class for every error raised deliberately by the framework."""


class ConfigurationError(DataFlowError):
    """Settings or a dataset contract are missing or invalid."""


class ExtractionError(DataFlowError):
    """A source could not be read at all (bad records are quarantined instead)."""


class SourceNotFoundError(ExtractionError):
    """A configured source file does not exist."""


class EmptySourceError(ExtractionError):
    """A source file has no content, not even a header row."""


class SchemaDriftError(DataFlowError):
    """A source no longer matches its contract, for example a required column is gone."""


class ConcurrentRunError(DataFlowError):
    """Another run of the same dataset holds the lock."""


class ReconciliationError(DataFlowError):
    """Source and target disagree in a way nothing explains, so nothing is published."""


class LoadError(DataFlowError):
    """A target write failed and its transaction was rolled back."""


class TransientDatabaseError(LoadError):
    """A failure that may succeed if retried, such as a dropped connection."""
