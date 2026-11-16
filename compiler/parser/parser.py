"""
parser/parser.py

Recursive-descent parser for the IoT schema language.
Consumes a token list from the Lexer and produces a Program AST.
"""

from __future__ import annotations
from typing import List, Optional

from lexer.tokenizer import Token, TT
from ast_nodes.nodes import (
    ASTNode, Program,
    MeasuresDef, DeviceClassNode,
    ColumnDef, TableNode,
    AttributeDef, FunctionalDep, EventTypeNode,
    ArrivalSpec, EventStreamNode,
    DistributionSpec, TierSpec, WhereBlock, DistributionNode,
    AggFunction, WindowSpec, SourceClause, Constraint, RuleNode,
    Term, FireTime,
)


class ParseError(Exception):
    def __init__(self, msg: str, token: Token):
        super().__init__(f'{msg} — got {token.type.name}({token.value!r}) '
                         f'at line {token.line}, col {token.column}')
        self.token = token


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos    = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else self.tokens[-1]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def expect(self, tt: TT) -> Token:
        if self.current.type != tt:
            raise ParseError(f'Expected {tt.name}', self.current)
        return self.advance()

    def match(self, *types: TT) -> bool:
        return self.current.type in types

    def consume_if(self, tt: TT) -> Optional[Token]:
        if self.current.type == tt:
            return self.advance()
        return None

    def skip_semicolons(self):
        while self.match(TT.SEMICOLON):
            self.advance()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def parse(self) -> Program:
        device_classes, tables, event_types = [], [], []
        event_streams, distributions, rules  = [], [], []

        while not self.match(TT.EOF):
            self.skip_semicolons()
            if self.match(TT.EOF):
                break

            if self.match(TT.CREATE):
                nxt = self.peek()
                if nxt.type == TT.DEVICE_CLASS or self._is_device_class_words(1):
                    device_classes.append(self._parse_device_class())
                elif nxt.type == TT.TABLE:
                    tables.append(self._parse_table())
                elif nxt.type == TT.EVENTTYPE:
                    event_types.append(self._parse_eventtype())
                elif nxt.type == TT.EVENTSTREAM:
                    event_streams.append(self._parse_eventstream())
                else:
                    raise ParseError('Unknown CREATE target', nxt)

            elif self.match(TT.DISTRIBUTION):
                distributions.append(self._parse_distribution())

            elif self.match(TT.NEW):
                rules.append(self._parse_rule())

            else:
                raise ParseError('Unexpected token at top level', self.current)

        return Program(
            device_classes=device_classes,
            tables=tables,
            event_types=event_types,
            event_streams=event_streams,
            distributions=distributions,
            rules=rules,
        )

    # ------------------------------------------------------------------
    # CREATE DEVICE_CLASS
    # ------------------------------------------------------------------

    def _is_device_class_words(self, offset: int) -> bool:
        """`DEVICE CLASS` written as two words (README and slides)."""
        a, b = self.peek(offset), self.peek(offset + 1)
        return (a.type == TT.IDENTIFIER and a.value.upper() == 'DEVICE' and
                b.type == TT.IDENTIFIER and b.value.upper() == 'CLASS')

    def _parse_device_class(self) -> DeviceClassNode:
        line = self.current.line
        self.expect(TT.CREATE)
        if self.match(TT.DEVICE_CLASS):
            self.advance()
        else:                                # DEVICE CLASS
            self.advance()
            self.advance()
        name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.LPAREN)

        measures    = []
        user_fields = []

        while not self.match(TT.RPAREN):
            if self.match(TT.MEASURES):
                measures.append(self._parse_measures())
            elif self.match(TT.ZONE, TT.IDENTIFIER) and self.peek().type == TT.ENUM:
                # User-defined field: `zone ENUM *` (any name, any case).
                # The value is resolved per device from the enterprise DB.
                field_name = self.advance().value.lower()
                self.expect(TT.ENUM)
                self.expect(TT.STAR)
                user_fields.append(field_name)
            else:
                raise ParseError(
                    f"Expected MEASURES or '<field> ENUM *' in DEVICE_CLASS {name}",
                    self.current)
            self.consume_if(TT.COMMA)

        self.expect(TT.RPAREN)
        self.skip_semicolons()
        return DeviceClassNode(name=name, measures=measures,
                               user_fields=user_fields, line=line)


    def _parse_signed_number(self) -> float:
        """Parse an optionally negative number."""
        sign = 1.0
        if self.match(TT.MINUS):
            self.advance()
            sign = -1.0
        return sign * float(self.advance().value)
    def _parse_measures(self) -> MeasuresDef:
        line = self.current.line
        self.expect(TT.MEASURES)
        name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.UNIT)
        if self.match(TT.LPAREN, TT.RPAREN, TT.COMMA, TT.EOF, TT.RANGE):
            raise ParseError('Expected a unit after UNIT', self.current)
        unit = self.advance().value          # "percent_VWC" or percent_VWC
        self.expect(TT.RANGE)
        self.expect(TT.LPAREN)
        rmin = self._parse_signed_number()
        self.expect(TT.COMMA)
        rmax = self._parse_signed_number()
        self.expect(TT.RPAREN)
        self.expect(TT.RESOLUTION)
        res  = float(self.advance().value)
        return MeasuresDef(name=name, unit=unit, range_min=rmin, range_max=rmax,
                           resolution=res)

    # ------------------------------------------------------------------
    # CREATE TABLE
    # ------------------------------------------------------------------

    def _parse_table(self) -> TableNode:
        line = self.current.line
        self.expect(TT.CREATE)
        self.expect(TT.TABLE)
        name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.LPAREN)
        columns = []
        while not self.match(TT.RPAREN):
            columns.append(self._parse_column())
            self.consume_if(TT.COMMA)
        self.expect(TT.RPAREN)
        self.skip_semicolons()
        return TableNode(name=name, columns=columns)

    def _parse_column(self) -> ColumnDef:
        line = self.current.line
        name = self._expect_word()

        # Data type
        if self.match(TT.INTEGER_TYPE, TT.FLOAT_TYPE, TT.STRING_TYPE, TT.ENUM_TYPE, TT.ENUM):
            dtype = self.advance().value.upper()
        else:
            dtype = self.advance().value.upper()

        primary_key = False
        references  = None
        nullable    = True

        # Optional modifiers
        while self.match(TT.PRIMARY, TT.REFERENCES):
            if self.match(TT.PRIMARY):
                self.advance()
                self.expect(TT.KEY)
                primary_key = True
                nullable    = False
            elif self.match(TT.REFERENCES):
                self.advance()
                # Handle REFERENCES DEVICE_CLASS (special keyword, no column)
                if self.match(TT.DEVICE_CLASS):
                    self.advance()
                    references = ('DEVICE_CLASS', 'name')
                else:
                    ref_table = self.expect(TT.IDENTIFIER).value
                    self.expect(TT.LPAREN)
                    ref_col   = self.expect(TT.IDENTIFIER).value
                    self.expect(TT.RPAREN)
                    references = (ref_table, ref_col)

        return ColumnDef(name=name, dtype=dtype, primary_key=primary_key,
                         references=references, nullable=nullable)

    # ------------------------------------------------------------------
    # CREATE EVENTTYPE
    # ------------------------------------------------------------------

    def _parse_eventtype(self) -> EventTypeNode:
        line = self.current.line
        self.expect(TT.CREATE)
        self.expect(TT.EVENTTYPE)
        name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.LPAREN)

        eventtime_attr = 'T'
        attributes     = []
        func_deps      = []

        ingest_attr, max_delay_ms = None, None

        while not self.match(TT.RPAREN):
            # EVENTTIME T INTEGER   (type optional)
            if self.match(TT.EVENTTIME):
                self.advance()
                eventtime_attr = self.expect(TT.IDENTIFIER).value
                if self.match(TT.INTEGER_TYPE, TT.FLOAT_TYPE):
                    self.advance()

            # INGESTTIME I MAXDELAY 5 MIN
            elif self.match(TT.IDENTIFIER) and self.current.value.upper() == 'INGESTTIME':
                self.advance()
                ingest_attr = self.expect(TT.IDENTIFIER).value
                if self.match(TT.INTEGER_TYPE, TT.FLOAT_TYPE):
                    self.advance()
                if not (self.match(TT.IDENTIFIER) and self.current.value.upper() == 'MAXDELAY'):
                    raise ParseError('Expected MAXDELAY <n> <unit> after INGESTTIME', self.current)
                self.advance()
                amount = self._parse_signed_number()
                max_delay_ms = self._to_ms(amount, self.advance())

            # Functional dependency: a, b --> c, d
            elif self._is_func_dep():
                func_deps.append(self._parse_func_dep())

            # Regular attribute, or a foreign key:
            #   device_id: Device(device_id) FK   |   device_id Devices(device_id) FOREIGNKEY
            else:
                attr_name = self._expect_word()       # also names like n, count, low
                self.consume_if(TT.COLON)
                if self.match(TT.IDENTIFIER) and self.peek().type == TT.LPAREN:
                    ref_table = self.advance().value
                    self.expect(TT.LPAREN)
                    ref_col = self.expect(TT.IDENTIFIER).value
                    self.expect(TT.RPAREN)
                    if not (self.match(TT.IDENTIFIER) and
                            self.current.value.upper() in ('FK', 'FOREIGNKEY')):
                        raise ParseError('Expected FK or FOREIGNKEY after '
                                         f'{ref_table}({ref_col})', self.current)
                    self.advance()
                    attributes.append(AttributeDef(name=attr_name, dtype='FK',
                                                   references=(ref_table, ref_col)))
                else:
                    attr_dtype = self.advance().value.upper()
                    attributes.append(AttributeDef(name=attr_name, dtype=attr_dtype))

            self.consume_if(TT.COMMA)

        self.expect(TT.RPAREN)
        self.skip_semicolons()
        return EventTypeNode(name=name, eventtime_attr=eventtime_attr,
                             attributes=attributes, func_deps=func_deps,
                             ingest_attr=ingest_attr, max_delay_ms=max_delay_ms,
                             line=line)

    def _is_func_dep(self) -> bool:
        """Look ahead to detect 'a, b --> c' pattern within current line."""
        i = self.pos
        limit = min(i + 8, len(self.tokens))  # look at most 8 tokens ahead
        while i < limit:
            tt = self.tokens[i].type
            if tt == TT.ARROW:
                return True
            if tt not in (TT.IDENTIFIER, TT.COMMA):
                return False
            i += 1
        return False

    def _parse_func_dep(self) -> FunctionalDep:
        line = self.current.line
        lhs  = [self.expect(TT.IDENTIFIER).value]
        while self.match(TT.COMMA) and self.peek().type == TT.IDENTIFIER:
            self.advance()
            lhs.append(self.expect(TT.IDENTIFIER).value)
        self.expect(TT.ARROW)
        rhs = [self.expect(TT.IDENTIFIER).value]
        while self.match(TT.COMMA) and self.peek().type == TT.IDENTIFIER:
            self.advance()
            rhs.append(self.expect(TT.IDENTIFIER).value)
        return FunctionalDep(lhs=lhs, rhs=rhs)

    # ------------------------------------------------------------------
    # CREATE EVENTSTREAM
    # ------------------------------------------------------------------

    def _parse_eventstream(self) -> EventStreamNode:
        line = self.current.line
        self.expect(TT.CREATE)
        self.expect(TT.EVENTSTREAM)
        name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.LPAREN)

        # EVENTTYPE et1, et2, ...
        self.expect(TT.EVENTTYPE)
        event_types = [self.expect(TT.IDENTIFIER).value]
        while self.match(TT.COMMA) and self.peek().type == TT.IDENTIFIER:
            self.advance()
            event_types.append(self.expect(TT.IDENTIFIER).value)

        # consume trailing comma after event types if present
        self.consume_if(TT.COMMA)
        # ARRIVALS ( ... )
        self.expect(TT.ARRIVALS)
        self.expect(TT.LPAREN)
        arrivals = []
        while not self.match(TT.RPAREN):
            arrivals.append(self._parse_arrival())
            self.consume_if(TT.COMMA)
        self.expect(TT.RPAREN)

        self.expect(TT.RPAREN)
        self.skip_semicolons()
        return EventStreamNode(name=name, event_types=event_types, arrivals=arrivals)

    def _parse_arrival(self) -> ArrivalSpec:
        line         = self.current.line
        measure_name = self.expect(TT.IDENTIFIER).value
        mode         = self.current.type.name
        interval_ms  = None
        threshold    = None

        if self.match(TT.PERIODIC):
            self.advance()
            self.expect(TT.LPAREN)
            value    = float(self.advance().value)
            unit_tok = self.advance()
            interval_ms = self._to_ms(value, unit_tok)
            self.expect(TT.RPAREN)
            mode = 'PERIODIC'

        elif self.match(TT.ON_CHANGE):
            self.advance()
            self.expect(TT.LPAREN)
            threshold = float(self.advance().value)
            self.expect(TT.RPAREN)
            mode = 'ON_CHANGE'

        elif self.match(TT.ON_THRESHOLD):
            self.advance()
            self.expect(TT.LPAREN)
            threshold = float(self.advance().value)
            self.expect(TT.RPAREN)
            mode = 'ON_THRESHOLD'

        failure_prob = 0.0
        if self.consume_if(TT.FAILURE):
            failure_prob = float(self.advance().value)

        return ArrivalSpec(measure_name=measure_name, mode=mode,
                           interval_ms=interval_ms, threshold=threshold,
                           failure_prob=failure_prob)

    _UNIT_MS = {
        'MS': 1,
        'SEC': 1000, 'SECOND': 1000, 'SECONDS': 1000,
        'MIN': 60_000, 'MINUTE': 60_000, 'MINUTES': 60_000,
        'HOUR': 3_600_000, 'HOURS': 3_600_000,
        'DAY': 86_400_000, 'DAYS': 86_400_000,
    }

    def _to_ms(self, value: float, unit_tok: Token) -> float:
        # Unknown units used to be read silently as seconds (so `1 HOUR` became
        # 1 second); they are now an error, and singular forms are accepted.
        unit = str(unit_tok.value).upper()
        if unit not in self._UNIT_MS:
            raise ParseError(f'Unknown time unit {unit_tok.value!r} '
                             f'(use MS, SEC, MIN, HOUR(S), DAY(S))', unit_tok)
        return value * self._UNIT_MS[unit]

    # ------------------------------------------------------------------
    # DISTRIBUTION FOR
    # ------------------------------------------------------------------

    def _parse_distribution(self) -> DistributionNode:
        line = self.current.line
        self.expect(TT.DISTRIBUTION)
        self.expect(TT.FOR)
        measure_name = self.expect(TT.IDENTIFIER).value
        self.expect(TT.LPAREN)

        where_blocks = []
        lifetime_ms  = None
        default_tiers_line = None
        default_tiers = []

        while not self.match(TT.RPAREN):
            if self.match(TT.FAILURE):
                # FAILURE 360 DAYS — mean sensor lifetime (slides)
                fail_tok = self.advance()
                amount = self._parse_signed_number()
                if self.match(TT.COMMA, TT.RPAREN, TT.WHERE) or self.current.line != fail_tok.line:
                    raise ParseError(
                        'FAILURE inside a DISTRIBUTION takes a duration '
                        '(e.g. FAILURE 360 DAYS); a per-reading drop probability '
                        'belongs in ARRIVALS: <Measure> PERIODIC (...) FAILURE <p>',
                        fail_tok)
                lifetime_ms = self._to_ms(amount, self.advance())
            elif self.match(TT.WHERE):
                # One block per discriminator value: WHERE <field> = <value> ( ... )
                where_blocks.append(self._parse_where_block())
            else:
                # No WHERE: tiers that apply to every device
                if default_tiers_line is None:
                    default_tiers_line = self.current.line
                default_tiers.extend(self._parse_tiers(stop_at_where=True))
            self.consume_if(TT.COMMA)

        if default_tiers and where_blocks:
            raise ParseError(f'DISTRIBUTION FOR {measure_name} mixes WHERE blocks with '
                             f'tiers outside any WHERE block', self.current)
        if default_tiers:
            where_blocks.append(WhereBlock(field=None, value=None,
                                           tiers=default_tiers, line=default_tiers_line))
        if not where_blocks:
            raise ParseError(f'DISTRIBUTION FOR {measure_name} declares no tiers', self.current)

        self.expect(TT.RPAREN)
        self.skip_semicolons()
        return DistributionNode(measure_name=measure_name, where_blocks=where_blocks,
                                line=line, failure_lifetime_ms=lifetime_ms)

    def _parse_where_block(self) -> WhereBlock:
        line = self.current.line
        self.expect(TT.WHERE)
        # <field> = <value>, where <field> is the device class's user-defined
        # field (ZONE / zone / any identifier) and <value> is an integer,
        # number, "string" or bare identifier, matched against the value
        # resolved from the enterprise DB.
        if not self.match(TT.ZONE, TT.IDENTIFIER):
            raise ParseError('Expected a field name after WHERE', self.current)
        field_name = self.advance().value.lower()
        self.expect(TT.EQUALS)
        value = self._parse_where_value()
        self.expect(TT.LPAREN)
        tiers = self._parse_tiers()
        self.expect(TT.RPAREN)
        return WhereBlock(field=field_name, value=value, tiers=tiers, line=line)

    def _parse_where_value(self):
        if self.match(TT.MINUS) or self.match(TT.INTEGER, TT.FLOAT):
            num = self._parse_signed_number()
            return int(num) if float(num).is_integer() else num
        if self.match(TT.STRING, TT.IDENTIFIER):
            return self.advance().value
        raise ParseError('Expected a value after WHERE <field> =', self.current)

    _DIST_TYPES = (TT.NORMAL, TT.UNIFORM, TT.EXPONENTIAL, TT.POISSON, TT.BINOMIAL)

    def _parse_tiers(self, stop_at_where: bool = False):
        """
        Tiers in any order (README scenarios put BELOW first):
            <DIST (...)> PROB p [STICKY]            the unlabelled NORMAL tier
            ABOVE ( <DIST (...)> PROB p [STICKY] )
            BELOW ( <DIST (...)> PROB p [STICKY] )
        separated by optional commas.
        """
        tiers = []
        while True:
            if self.match(TT.ABOVE, TT.BELOW):
                tier_name = self.advance().value.upper()
                self.expect(TT.LPAREN)
                tiers.append(self._parse_one_tier(tier_name))
                self.consume_if(TT.COMMA)
                self.expect(TT.RPAREN)
            elif self.match(*self._DIST_TYPES):
                tiers.append(self._parse_one_tier('NORMAL'))
            else:
                break
            self.consume_if(TT.COMMA)
            if stop_at_where and self.match(TT.WHERE, TT.FAILURE):
                break
        if not tiers:
            raise ParseError('Expected a distribution tier', self.current)
        return tiers

    def _parse_one_tier(self, tier_name: str) -> TierSpec:
        dist = self._parse_dist_spec()
        self.expect(TT.PROB)
        prob = float(self.advance().value)
        sticky = False
        if self.match(TT.IDENTIFIER) and self.current.value.upper() == 'STICKY':
            self.advance()
            sticky = True
        return TierSpec(tier=tier_name, distribution=dist, probability=prob, sticky=sticky)

    def _parse_dist_spec(self) -> DistributionSpec:
        line = self.current.line
        dist_type = self.advance().value.upper()
        self.expect(TT.LPAREN)
        params = {}
        while not self.match(TT.RPAREN):
            key = self.advance().value.lower()
            self.expect(TT.EQUALS)
            val = self._parse_signed_number()   # accepts e.g. mean = -2.0
            params[key] = val
            self.consume_if(TT.COMMA)
        self.expect(TT.RPAREN)
        return DistributionSpec(dist_type=dist_type, params=params)

    # ------------------------------------------------------------------
    # IoTDL rules
    # ------------------------------------------------------------------

    def _parse_rule(self) -> RuleNode:
        """
        new <event>[<window>](<head args>) @ (<fire time>) :- <body>;

        The window is optional (README: "no window — fires immediately on each
        matching event"). Window sizes and fire-time offsets may carry a unit:
            tumbling(s, 30 MIN)   @ (s + 31 MIN)
        """
        line = self.current.line
        self.expect(TT.NEW)
        head_event = self.expect(TT.IDENTIFIER).value

        window = None
        if self.consume_if(TT.LBRACKET):
            wtok = self.advance()
            wtype = str(wtok.value).lower()
            if wtype not in ('sliding', 'tumbling'):
                raise ParseError(f'Unknown window type {wtok.value!r} '
                                 f'(use sliding or tumbling)', wtok)
            self.expect(TT.LPAREN)
            slide_var = self.expect(TT.IDENTIFIER).value
            self.expect(TT.COMMA)
            size = self._parse_signed_number()
            size_ms = None
            if not self.match(TT.RPAREN):
                size_ms = self._to_ms(size, self.advance())
            self.expect(TT.RPAREN)
            self.expect(TT.RBRACKET)
            window = WindowSpec(window_type=wtype, slide_var=slide_var,
                                size=size, size_ms=size_ms)

        # Head args: (device_id, DROUGHT_STRESS, (value = avg(value)))
        self.expect(TT.LPAREN)
        head_args: List[Term] = []
        agg_functions = []
        while not self.match(TT.RPAREN):
            if self.match(TT.LPAREN):
                # (out = func(in) [, out2 = func2(in2) ...])
                self.advance()
                while True:
                    out_var = self._expect_word()
                    self.expect(TT.EQUALS)
                    func    = str(self.advance().value).lower()
                    self.expect(TT.LPAREN)
                    in_var  = self.advance().value
                    self.expect(TT.RPAREN)
                    agg_functions.append(AggFunction(output_var=out_var, func=func,
                                                     input_var=in_var))
                    if not self.consume_if(TT.COMMA):
                        break
                self.expect(TT.RPAREN)
            else:
                head_args.append(self._parse_term())
            if not self.match(TT.RPAREN):
                self.expect(TT.COMMA)
        self.expect(TT.RPAREN)

        # @ (<var> [+/- <number> [<unit>]]...)
        self.expect(TT.AT)
        fire_time = self._parse_fire_time()

        # :- body
        self.expect(TT.TURNSTILE)
        sources, constraints = self._parse_body()
        self.skip_semicolons()

        return RuleNode(
            head_event=head_event, window=window,
            grouping_vars=[t.value for t in head_args if t.kind == 'ident'],
            agg_functions=agg_functions,
            fire_time_expr=fire_time.text, sources=sources,
            constraints=constraints, head_args=head_args,
            fire_time=fire_time, line=line,
        )

    @staticmethod
    def _is_word(tok: Token) -> bool:
        """An identifier, or a keyword spelled like one (n, p, min, count ...)."""
        return (tok.type not in (TT.STRING, TT.INTEGER, TT.FLOAT, TT.EOF) and
                isinstance(tok.value, str) and tok.value[:1].isalpha())

    def _expect_word(self) -> str:
        if not self._is_word(self.current):
            raise ParseError('Expected a name', self.current)
        return self.advance().value

    def _parse_term(self) -> Term:
        if self.match(TT.UNDERSCORE):
            self.advance()
            return Term(kind='wild', value='_')
        if self.match(TT.MINUS) or self.match(TT.INTEGER, TT.FLOAT):
            num = self._parse_signed_number()
            return Term(kind='const', value=int(num) if float(num).is_integer() else num)
        if self.match(TT.STRING):
            return Term(kind='const', value=self.advance().value)
        tok = self.current
        if tok.type in (TT.LPAREN, TT.RPAREN, TT.COMMA, TT.AT, TT.EOF, TT.SEMICOLON):
            raise ParseError('Expected a variable, constant or _', tok)
        # identifiers and keyword-looking words (e.g. MIN, NORMAL) used as constants
        return Term(kind='ident', value=self.advance().value)

    def _parse_fire_time(self) -> FireTime:
        self.expect(TT.LPAREN)
        start = self.pos
        var = self.expect(TT.IDENTIFIER).value
        offsets = []
        while self.match(TT.PLUS, TT.MINUS):
            sign = 1 if self.advance().type == TT.PLUS else -1
            num = float(self.advance().value)
            unit_ms = None
            if not self.match(TT.PLUS, TT.MINUS, TT.RPAREN):
                unit_ms = self._to_ms(1.0, self.advance())
            offsets.append((sign, num, unit_ms))
        text = ' '.join(str(t.value) for t in self.tokens[start:self.pos])
        self.expect(TT.RPAREN)
        return FireTime(var=var, offsets=offsets, text=text)

    _OPS = {TT.LT: '<', TT.LE: '<=', TT.GT: '>', TT.GE: '>=',
            TT.EQUALS: '==', TT.EQEQ: '==', TT.NE: '!='}

    def _parse_body(self):
        sources     = []
        constraints = []

        while True:
            # Source clause: event_name(args) @ z
            if self.match(TT.IDENTIFIER) and self.peek().type == TT.LPAREN:
                ev_name = self.advance().value
                self.expect(TT.LPAREN)
                args: List[Term] = []
                while not self.match(TT.RPAREN):
                    args.append(self._parse_term())
                    if not self.match(TT.RPAREN):
                        self.expect(TT.COMMA)
                self.expect(TT.RPAREN)
                self.expect(TT.AT)
                time_var = self.expect(TT.IDENTIFIER).value
                sources.append(SourceClause(event_name=ev_name,
                                            bound_vars=[str(a.value) for a in args],
                                            time_var=time_var, args=args))

            # Constraint: value < 25
            elif self._is_word(self.current) and self.peek().type in self._OPS:
                lhs_tok = self.advance()
                op_tok  = self.current
                if op_tok.type not in self._OPS:
                    raise ParseError('Expected a comparison (<, <=, >, >=, =, ==, !=)', op_tok)
                self.advance()
                rhs = self._parse_term()
                constraints.append(Constraint(lhs=lhs_tok.value, op=self._OPS[op_tok.type],
                                              rhs=rhs))

            else:
                raise ParseError('Expected a source clause or a constraint', self.current)

            if self.match(TT.COMMA):
                self.advance()
            else:
                break

        return sources, constraints
