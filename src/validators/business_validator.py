"""Applies a table's declarative rules. Null values pass: required fields check those."""

import operator
from collections.abc import Callable

import numpy as np
import pandas as pd

from src.core.contract import RuleSpec, TableContract
from src.validators.result import IssueCollector

_COMPARE = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}

CustomRule = Callable[[str, pd.DataFrame], pd.Series]


def _failing_mask(rule: RuleSpec, frame: pd.DataFrame, custom: CustomRule) -> np.ndarray:
    if rule.check == "custom":
        return custom(rule.name, frame).fillna(False).to_numpy(dtype=bool)

    if rule.check == "compare":
        left, right = frame[rule.left], frame[rule.right]
        present = left.notna() & right.notna()
        passes = _COMPARE[rule.op](left[present], right[present])
        return (present & ~passes.reindex(frame.index, fill_value=True)).to_numpy(dtype=bool)

    values = frame[rule.column]
    present = values.notna()
    if rule.check == "range":
        numbers = values[present].astype("float64")
        failing = pd.Series(False, index=numbers.index)
        if rule.min is not None:
            failing |= numbers <= rule.min if rule.min_exclusive else numbers < rule.min
        if rule.max is not None:
            failing |= numbers > rule.max
    elif rule.check == "allowed_values":
        failing = ~values[present].isin(rule.values)
    else:  # regex
        failing = ~values[present].astype(str).str.fullmatch(rule.pattern)
    return failing.reindex(frame.index, fill_value=False).to_numpy(dtype=bool)


def apply_rules(frame: pd.DataFrame, table: TableContract, issues: IssueCollector, custom: CustomRule) -> None:
    for rule in table.rules:
        issues.add(rule.name, rule.severity, _failing_mask(rule, frame, custom), rule.column, rule.message)
