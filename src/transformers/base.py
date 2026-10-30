"""Transformation contract. Implementations must be deterministic: same input, same output."""

import importlib

import pandas as pd

from src.core.contract import TableContract
from src.core.exceptions import ConfigurationError


class BaseTransformer:
    """Dispatches to ``transform_<table>`` and ``rule_<name>`` methods when they exist."""

    def transform(self, table: TableContract, frame: pd.DataFrame) -> pd.DataFrame:
        method = getattr(self, f"transform_{table.name}", None)
        result = method(frame.copy()) if method else frame
        missing = [c.name for c in table.all_columns if c.name not in result.columns]
        if missing:
            raise ConfigurationError(f"Transformer did not produce columns for {table.name}: {missing}")
        return result

    def custom_rule(self, name: str, frame: pd.DataFrame) -> pd.Series:
        """Return a mask of the rows that fail the named rule."""
        method = getattr(self, f"rule_{name}", None)
        if method is None:
            raise ConfigurationError(f"{type(self).__name__} has no custom rule {name}")
        return method(frame)


def load_transformer(path: str) -> BaseTransformer:
    """Load ``module:Class`` from a contract. Only this project's package is allowed."""
    module_name, _, class_name = path.partition(":")
    if not module_name.startswith("src.transformers."):
        raise ConfigurationError(f"Transformer must live in src.transformers: {path}")
    try:
        return getattr(importlib.import_module(module_name), class_name)()
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(f"Cannot load transformer {path}: {exc}") from exc
