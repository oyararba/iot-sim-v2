"""
ast_nodes/nodes.py

AST node dataclasses for every construct in the IoT schema language.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------------------------------------------------
# Base
# -----------------------------------------------------------------------

@dataclass
class ASTNode:
    pass


# -----------------------------------------------------------------------
# Device Class
# -----------------------------------------------------------------------

@dataclass
class MeasuresDef(ASTNode):
    name:       str          # e.g. "Moisture"
    unit:       str          # e.g. "percent_VWC"
    range_min:  float        # physical minimum
    range_max:  float        # physical maximum
    resolution: float        # smallest detectable change


@dataclass
class DeviceClassNode(ASTNode):
    name:        str
    measures:    List[MeasuresDef]
    # User-defined fields declared as `<name> ENUM *` (e.g. `zone ENUM *`).
    # The language allows one; the validator reports extras. The field is the
    # distribution discriminator: DISTRIBUTION ... WHERE <name> = <value>
    # blocks are selected by the value resolved for each device from the
    # enterprise DB tables. Names are stored lower-case.
    user_fields: List[str] = field(default_factory=list)
    line:        int = 0

    @property
    def discriminator(self) -> Optional[str]:
        return self.user_fields[0] if self.user_fields else None

    @property
    def zone_enum(self) -> bool:
        """Backwards-compatible flag: True if a user-defined field is declared."""
        return bool(self.user_fields)


# -----------------------------------------------------------------------
# Table
# -----------------------------------------------------------------------

@dataclass
class ColumnDef(ASTNode):
    name:        str
    dtype:       str          # INTEGER | FLOAT | STRING | ENUM
    primary_key: bool = False
    references:  Optional[Tuple[str, str]] = None   # (table, column)
    nullable:    bool = True


@dataclass
class TableNode(ASTNode):
    name:    str
    columns: List[ColumnDef]


# -----------------------------------------------------------------------
# Event Type
# -----------------------------------------------------------------------

@dataclass
class AttributeDef(ASTNode):
    name:  str
    dtype: str
    # `device_id: Device(device_id) FK` — the enterprise table/column it points at
    references: Optional[Tuple[str, str]] = None


@dataclass
class FunctionalDep(ASTNode):
    lhs: List[str]
    rhs: List[str]


@dataclass
class EventTypeNode(ASTNode):
    name:          str
    eventtime_attr: str                      # name of the EVENTTIME attribute
    attributes:    List[AttributeDef]
    func_deps:     List[FunctionalDep]
    # INGESTTIME I MAXDELAY 5 MIN
    ingest_attr:   Optional[str]   = None
    max_delay_ms:  Optional[float] = None
    line:          int = 0


# -----------------------------------------------------------------------
# Event Stream
# -----------------------------------------------------------------------

@dataclass
class ArrivalSpec(ASTNode):
    measure_name: str                        # e.g. "Moisture"
    mode:         str                        # PERIODIC | ON_CHANGE | ON_THRESHOLD
    interval_ms:  Optional[float]            # for PERIODIC — converted to ms
    threshold:    Optional[float]            # for ON_CHANGE and ON_THRESHOLD
    failure_prob: float = 0.0                # 0.0 to 1.0; FAILURE is optional (slides omit it)


@dataclass
class EventStreamNode(ASTNode):
    name:        str
    event_types: List[str]                   # list of event type names
    arrivals:    List[ArrivalSpec]


# -----------------------------------------------------------------------
# Distribution
# -----------------------------------------------------------------------

@dataclass
class DistributionSpec(ASTNode):
    """One statistical distribution with its parameters."""
    dist_type: str                           # NORMAL | UNIFORM | EXPONENTIAL | POISSON | BINOMIAL
    params:    Dict[str, float]              # e.g. {"mean": 65.0, "std_dev": 8.0}


@dataclass
class TierSpec(ASTNode):
    """NORMAL / ABOVE / BELOW tier within a WHERE block."""
    tier:          str                       # NORMAL | ABOVE | BELOW
    distribution:  DistributionSpec
    probability:   float
    sticky:        bool = False              # STICKY: transitions into/out of this tier are gradual


@dataclass
class WhereBlock(ASTNode):
    """
    `WHERE <field> = <value> ( ... )` block, or the single unconditional block
    of a DISTRIBUTION written without WHERE (field and value are then None and
    the block applies to every device).
    """
    field:     Optional[str]                 # discriminator name, lower-case; None = applies to all
    value:     Any                           # discriminator value (int, float or str); None = applies to all
    tiers:     List[TierSpec]                # NORMAL + optional ABOVE + optional BELOW
    line:      int = 0

    @property
    def zone_id(self) -> Any:
        """Backwards-compatible alias for `value`."""
        return self.value

    @property
    def is_default(self) -> bool:
        return self.field is None


@dataclass
class DistributionNode(ASTNode):
    measure_name: str                        # matches a MEASURES name in a DEVICE_CLASS
    where_blocks: List[WhereBlock]           # one per discriminator value, or one default block
    line:         int = 0
    # FAILURE 360 DAYS — mean lifetime of the sensor; it stops reporting when it fails
    failure_lifetime_ms: Optional[float] = None

    @property
    def is_unconditional(self) -> bool:
        return len(self.where_blocks) == 1 and self.where_blocks[0].is_default

    @property
    def discriminator(self) -> Optional[str]:
        fields = {wb.field for wb in self.where_blocks if wb.field}
        return next(iter(fields)) if len(fields) == 1 else None


# -----------------------------------------------------------------------
# IoTDL Rules
# -----------------------------------------------------------------------

@dataclass
class AggFunction(ASTNode):
    output_var: str                          # e.g. "avg_value"
    func:       str                          # avg | min | max | count | sum | last
    input_var:  str                          # e.g. "value"


@dataclass
class Term(ASTNode):
    """
    One argument in a rule head or body.
      kind = 'wild'  — `_`
      kind = 'const' — a number or "string"
      kind = 'ident' — an identifier; whether it is a variable or a constant
                       (Moisture, DROUGHT_STRESS) is decided by the code
                       generator from how it is used in the rule
    """
    kind:  str
    value: Any = None


@dataclass
class WindowSpec(ASTNode):
    window_type: str                         # sliding | tumbling
    slide_var:   str                         # s
    size:        float                       # bare number: readings (sliding) or ticks (tumbling)
    size_ms:     Optional[float] = None      # set when a unit is given: sliding(s, 6 HOURS)


@dataclass
class FireTime(ASTNode):
    """@ (s + 31 MIN) — a variable plus/minus offsets (bare numbers are ticks)."""
    var:        str
    offsets:    List[Tuple[int, float, Optional[float]]] = field(default_factory=list)
    #           (sign, number, ms per unit or None for ticks)
    text:       str = ''


@dataclass
class SourceClause(ASTNode):
    event_name: str
    bound_vars: List[str]                    # positional bindings (as written)
    time_var:   str                          # z
    args:       List[Term] = field(default_factory=list)


@dataclass
class Constraint(ASTNode):
    lhs: str
    op:  str                                 # < | > | <= | >= | == | !=
    rhs: Any                                 # Term (number, "string" or identifier)


@dataclass
class RuleNode(ASTNode):
    head_event:    str
    window:        Optional[WindowSpec]      # None = fires on each matching event
    grouping_vars: List[str]
    agg_functions: List[AggFunction]
    fire_time_expr: str
    sources:       List[SourceClause]
    constraints:   List[Constraint]
    head_args:     List[Term] = field(default_factory=list)   # non-aggregation head args, in order
    fire_time:     Optional[FireTime] = None
    line:          int = 0


@dataclass
class Program(ASTNode):
    device_classes: List[DeviceClassNode]
    tables:         List[TableNode]
    event_types:    List[EventTypeNode]
    event_streams:  List[EventStreamNode]
    distributions:  List[DistributionNode]
    rules:          List[RuleNode]
