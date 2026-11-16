"""Plain-English question parser for DropAPI's /ask endpoint.

No AI model is involved. The parser looks for column names, known values,
comparison words and a few ranking words, and produces a small plan made only
of schema fields and allow-listed operators. The API validates that plan with
exactly the same checks it uses for ordinary query-string filters.
"""
import re

MAX_QUESTION_LEN = 300
MAX_LIMIT = 100

NUM = r"-?\d+(?:\.\d+)?"
DATE = r"\d{4}-\d{2}-\d{2}"
END_NUM = r"(?!\d|\.\d|-\d)"
B_START = r"(?<![A-Za-z0-9_])"
B_END = r"(?![A-Za-z0-9_])"
RANGE_TYPES = ("integer", "number", "date")

OPERATORS = {
    "gte": ["at least", "no less than", "greater than or equal to",
            "minimum of", "since", ">="],
    "lte": ["at most", "no more than", "less than or equal to",
            "maximum of", "up to", "<="],
    "gt": ["greater than", "more than", "higher than", "bigger than",
           "larger than", "older than", "later than", "over", "above",
           "exceeding", "exceeds", "after", ">"],
    "lt": ["less than", "fewer than", "lower than", "smaller than",
           "younger than", "earlier than", "under", "below", "before", "<"],
    "eq": ["equal to", "equals", "exactly", "=", ":"],
}
PHRASE_OP = {p: op for op, ps in OPERATORS.items() for p in ps}
_ORDERED = sorted(PHRASE_OP, key=len, reverse=True)
_WORDS = "|".join(re.escape(p) for p in _ORDERED if p[0].isalpha())
_SYMBOLS = "|".join(re.escape(p) for p in _ORDERED if not p[0].isalpha())
OP = r"((?<![A-Za-z])(?:" + _WORDS + r")(?![A-Za-z])|" + _SYMBOLS + r")"

TEXT_OPS = {
    "contains": "contains", "containing": "contains",
    "includes": "contains", "including": "contains",
    "starts with": "begins_with", "starting with": "begins_with",
    "begins with": "begins_with", "beginning with": "begins_with",
}
DIR_ASC = ("ascending", "asc", "lowest", "smallest", "least", "fewest",
           "minimum", "earliest", "cheapest", "bottom")
DIR_DESC = ("descending", "desc", "highest", "largest", "biggest", "greatest",
            "most expensive", "most", "maximum", "latest", "newest", "recent",
            "priciest", "costliest", "top")
PRICE_WORDS = ("price", "cost", "amount", "fee", "total", "salary", "revenue")
NAME_COLUMNS = {"name", "full_name", "fullname", "customer", "customer_name",
                "student", "employee", "person", "title", "product",
                "product_name"}


class _Plan:
    def __init__(self, text):
        self.text = text
        self.used = []
        self.filters = {}
        self.notes = []
        self.sort = None
        self.limit = None
        self.hint = None

    def free(self, start, end):
        return all(end <= s or start >= e for s, e in self.used)

    def take(self, start, end):
        self.used.append((start, end))

    def add(self, field, op, value):
        key = (field, op)
        if key in self.filters:
            if self.filters[key] != value:
                self.notes.append(
                    f"Only one value per column is supported, so I ignored "
                    f"a second value for '{field}'.")
            return
        self.filters[key] = value


# ---------- small helpers ----------

def clean(question):
    text = question.replace("\u2019", "'").replace("+", " ")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)   # 1,000 -> 1000
    text = re.sub(r"[$\u20b9\u20ac\u00a3]", "", text)  # currency symbols
    text = re.sub(r"[?!;,\"]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _typed(raw):
    return int(raw) if re.fullmatch(r"-?\d+", raw) else float(raw)


def _norm(text):
    return re.sub(r"[ _]+", " ", text.lower())


def _forms(col):
    base = col["name"].lower().strip()
    spaced = re.sub(r"[_\-\s]+", " ", base).strip()
    forms = {base, spaced}
    if len(spaced) > 3 and spaced.endswith("s"):
        forms.add(spaced[:-1])
    else:
        forms.add(spaced + "s")
    return sorted((f for f in forms if f), key=len, reverse=True)


def _body(form):
    return r"[ _]".join(re.escape(part) for part in form.split(" "))


def _phrase_re(form):
    return B_START + _body(form) + B_END


def _phrases(schema):
    found = [(form, col) for col in schema["columns"] for form in _forms(col)]
    found.sort(key=lambda item: len(item[0]), reverse=True)
    return found


def _resolve_case(plan, col, token, op):
    """DynamoDB text matching is case-sensitive, so reuse the stored casing."""
    values = col.get("values")
    if values:
        lowered = token.lower()
        for stored in values:
            idx = stored.lower().find(lowered)
            if idx == -1 or (op == "begins_with" and idx != 0):
                continue
            return stored[idx: idx + len(token)]
        return token
    plan.notes.append(f"Text matching on '{col['name']}' is case-sensitive.")
    return token


# ---------- parsing steps ----------

def _limit(plan):
    patterns = [
        (r"(top|first|bottom|limit)\s+(\d{1,4})", 1, 2),
        (r"(\d{1,4})\s+(cheapest|most expensive|priciest|highest|lowest|"
         r"largest|smallest|biggest|latest|newest|earliest)", 2, 1),
    ]
    for pattern, word_i, num_i in patterns:
        m = re.search(B_START + pattern + B_END, plan.text, re.I)
        if not m:
            continue
        n = int(m.group(num_i))
        plan.take(m.start(num_i), m.end(num_i))
        if n < 1:
            return
        if n > MAX_LIMIT:
            plan.notes.append(f"Limit reduced to {MAX_LIMIT}.")
            n = MAX_LIMIT
        plan.limit = n
        word = m.group(word_i).lower()
        if word in ("bottom", "lowest", "cheapest", "smallest", "earliest"):
            plan.hint = "asc"
        elif word in ("top", "highest", "most expensive", "priciest",
                      "largest", "biggest", "latest", "newest"):
            plan.hint = "desc"
        return


def _between(plan, phrases):
    for form, col in phrases:
        if col["type"] not in RANGE_TYPES:
            continue
        is_date = col["type"] == "date"
        val = DATE if is_date else NUM
        pattern = (_phrase_re(form) + r"\s*(?:is|are|was|were)?\s*between\s+("
                   + val + r")\s+and\s+(" + val + r")" + END_NUM)
        for m in re.finditer(pattern, plan.text, re.I):
            if not plan.free(m.start(), m.end()):
                continue
            lo, hi = m.group(1), m.group(2)
            plan.add(col["name"], "gte", lo if is_date else _typed(lo))
            plan.add(col["name"], "lte", hi if is_date else _typed(hi))
            plan.take(m.start(), m.end())


def _comparisons(plan, phrases):
    for form, col in phrases:
        if col["type"] not in RANGE_TYPES:
            continue
        is_date = col["type"] == "date"
        val = DATE if is_date else NUM
        lead = _phrase_re(form) + r"\s*(?:is|are|was|were|of)?\s*"
        patterns = [
            lead + OP + r"\s*(" + val + r")" + END_NUM,
            lead + r"()(" + val + r")" + END_NUM,   # e.g. "age 29"
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, plan.text, re.I):
                if not plan.free(m.start(), m.end()):
                    continue
                op = PHRASE_OP[m.group(1).lower()] if m.group(1) else "eq"
                raw = m.group(2)
                plan.add(col["name"], op, raw if is_date else _typed(raw))
                plan.take(m.start(), m.end())


def _booleans(plan, cols):
    for col in cols:
        if col["type"] != "boolean":
            continue
        forms = _forms(col)
        handled = False
        for form in forms:
            m = re.search(_phrase_re(form)
                          + r"\s*(?:is|are|=|:|equals?)?\s*(true|false|yes|no)"
                          + B_END, plan.text, re.I)
            if m and plan.free(m.start(), m.end()):
                plan.add(col["name"], "eq",
                         m.group(1).lower() in ("true", "yes"))
                plan.take(m.start(), m.end())
                handled = True
                break
        if handled:
            continue
        for form in forms:      # "not active", "inactive", "unpaid"
            m = re.search(r"(?<![A-Za-z])(?:(?:not|never|isn't|aren't|non)[ -]+"
                          r"|in|un|non)" + _body(form) + B_END,
                          plan.text, re.I)
            if m and plan.free(m.start(), m.end()):
                plan.add(col["name"], "eq", False)
                plan.take(m.start(), m.end())
                handled = True
                break
        if handled:
            continue
        for form in forms:
            m = re.search(_phrase_re(form), plan.text, re.I)
            if m and plan.free(m.start(), m.end()):
                plan.add(col["name"], "eq", True)
                plan.take(m.start(), m.end())
                break


def _values(plan, cols):
    negation = (r"(?:^|\s)(?:not|except|excluding|exclude|other than|outside|"
                r"besides|apart from|without)\s+(?:(?:in|from|the|of)\s+){0,2}$")
    for col in cols:
        if col["type"] != "string" or not col.get("values"):
            continue
        for value in sorted(col["values"], key=len, reverse=True):
            if len(value) < 2:
                continue
            pattern = (r"(?<![A-Za-z0-9])" + re.escape(value)
                       + r"(?![A-Za-z0-9])")
            for m in re.finditer(pattern, plan.text, re.I):
                if not plan.free(m.start(), m.end()):
                    continue
                before = plan.text[max(0, m.start() - 30):m.start()]
                is_neg = re.search(negation, before, re.I) is not None
                plan.add(col["name"], "neq" if is_neg else "eq", value)
                plan.take(m.start(), m.end())
                break


def _text_match(plan, phrases):
    words = "|".join(re.escape(w) for w in
                     sorted(TEXT_OPS, key=len, reverse=True))
    for form, col in phrases:
        if col["type"] != "string":
            continue
        pattern = (_phrase_re(form) + r"\s+(?:that\s+|which\s+)?(" + words
                   + r")\s+(\S+)")
        for m in re.finditer(pattern, plan.text, re.I):
            if not plan.free(m.start(), m.end()):
                continue
            token = m.group(2).strip("\".'")
            if not token:
                continue
            op = TEXT_OPS[m.group(1).lower()]
            plan.add(col["name"], op, _resolve_case(plan, col, token, op))
            plan.take(m.start(), m.end())


def _named(plan, cols):
    m = re.search(B_START + r"(?:named|called)\s+([A-Za-z0-9'.\-]+)",
                  plan.text, re.I)
    if not m or not plan.free(m.start(), m.end()):
        return
    candidates = [
        c for c in cols
        if c["type"] == "string"
        and (c["name"].lower() in NAME_COLUMNS
             or c["name"].lower().endswith("name"))
        and (c["name"], "eq") not in plan.filters
    ]
    if not candidates:
        return
    col = candidates[0]
    token = m.group(1).strip(".'")
    plan.add(col["name"], "contains",
             _resolve_case(plan, col, token, "contains"))
    plan.take(m.start(), m.end())


def _dates(plan, cols):
    date_cols = [c for c in cols if c["type"] == "date"]
    if len(date_cols) != 1:
        return
    name = date_cols[0]["name"]
    m = re.search(B_START + r"(?:in|during)\s+(\d{4})" + END_NUM,
                  plan.text, re.I)
    if m and plan.free(m.start(), m.end()) and 1900 <= int(m.group(1)) <= 2100:
        year = m.group(1)
        plan.add(name, "gte", year + "-01-01")
        plan.add(name, "lte", year + "-12-31")
        plan.take(m.start(), m.end())
    m = re.search(B_START + r"on\s+(" + DATE + r")", plan.text, re.I)
    if m and plan.free(m.start(), m.end()):
        plan.add(name, "eq", m.group(1))
        plan.take(m.start(), m.end())


def _loose_comparisons(plan, schema):
    """'over 30' with no column named: only guess when there is one candidate."""
    cols = schema["columns"]
    pk = schema.get("primary_key")
    numeric = [c for c in cols
               if c["type"] in ("integer", "number") and c["name"] != pk]
    dates = [c for c in cols if c["type"] == "date"]
    pattern = OP + r"\s*(" + DATE + "|" + NUM + r")" + END_NUM
    for m in re.finditer(pattern, plan.text, re.I):
        phrase = m.group(1).lower()
        op = PHRASE_OP[phrase]
        if op == "eq" or not plan.free(m.start(), m.end()):
            continue
        raw = m.group(2)
        is_date = re.fullmatch(DATE, raw) is not None
        candidates = dates if is_date else numeric
        if not is_date and phrase in ("older than", "younger than"):
            ages = [c for c in numeric if c["name"].lower() == "age"]
            candidates = ages or candidates
        if len(candidates) == 1:
            plan.add(candidates[0]["name"], op,
                     raw if is_date else _typed(raw))
        elif candidates:
            plan.notes.append(
                f"I couldn't tell which column '{phrase} {raw}' refers to. "
                f"Name it, e.g. '{candidates[0]['name']} {phrase} {raw}'.")
        else:
            continue
        plan.take(m.start(), m.end())


def _direction(plan):
    text = plan.text.lower()
    explicit = re.search(B_START + r"(ascending|descending|asc|desc)" + B_END,
                         text)
    if explicit:
        return "asc" if explicit.group(1).startswith("asc") else "desc"
    words = sorted(DIR_ASC + DIR_DESC, key=len, reverse=True)
    m = re.search(B_START + "(" + "|".join(re.escape(w) for w in words)
                  + ")" + B_END, text)
    if m:
        return "asc" if m.group(1) in DIR_ASC else "desc"
    return plan.hint or "asc"


def _sort(plan, phrases, cols):
    lookup = {_norm(form): col for form, col in phrases}
    alt = "|".join(_body(form) for form, _ in phrases)

    def find(pattern, kinds=None):
        m = re.search(pattern, plan.text, re.I)
        if not m:
            return None
        col = lookup.get(_norm(m.group(1)))
        if col and (kinds is None or col["type"] in kinds):
            return col
        return None

    ranked = (r"(?:sorted|sort|ordered|order|ranked|rank|arranged)\s+by\s+("
              + alt + r")" + B_END)
    by = r"(?<![A-Za-z])by\s+(" + alt + r")" + B_END
    superlative = (r"(?<![A-Za-z])(?:highest|largest|biggest|greatest|most|"
                   r"lowest|smallest|least|fewest|latest|newest|earliest|top|"
                   r"bottom|maximum|minimum)\s+(" + alt + r")" + B_END)
    trailing = (B_START + "(" + alt + r")\s+(?:descending|ascending|desc|asc)"
                + B_END)

    col = (find(ranked) or find(by) or find(superlative, RANGE_TYPES)
           or find(trailing))
    direction = None

    if col is None:
        m = re.search(B_START + r"(cheapest|most expensive|priciest|costliest)"
                      + B_END, plan.text, re.I)
        if m:
            price_cols = [c for c in cols
                          if c["type"] in ("integer", "number")
                          and any(w in c["name"].lower() for w in PRICE_WORDS)]
            if price_cols:
                col = price_cols[0]
                direction = "asc" if m.group(1).lower() == "cheapest" else "desc"

    if col is None:
        m = re.search(B_START + r"(latest|newest|most recent|recent|earliest)"
                      + B_END, plan.text, re.I)
        date_cols = [c for c in cols if c["type"] == "date"]
        if m and len(date_cols) == 1:
            col = date_cols[0]
            direction = "asc" if m.group(1).lower() == "earliest" else "desc"

    if col is not None:
        plan.sort = {"field": col["name"],
                     "direction": direction or _direction(plan)}
    elif plan.hint:
        plan.notes.append("Say what to rank by, e.g. 'top 3 by <column>'.")


# ---------- public API ----------

def parse_question(question, schema):
    plan = _Plan(clean(question[:MAX_QUESTION_LEN]))
    cols = schema["columns"]
    phrases = _phrases(schema)
    _limit(plan)
    _between(plan, phrases)
    _comparisons(plan, phrases)
    _booleans(plan, cols)
    _values(plan, cols)
    _text_match(plan, phrases)
    _named(plan, cols)
    _dates(plan, cols)
    _loose_comparisons(plan, schema)
    _sort(plan, phrases, cols)
    return {
        "filters": [{"field": f, "op": o, "value": v}
                    for (f, o), v in plan.filters.items()],
        "sort": plan.sort,
        "limit": plan.limit,
        "notes": plan.notes,
    }


def to_params(plan):
    """Turn a plan into the same query-string parameters the API already validates."""
    params = {}
    for f in plan["filters"]:
        value = f["value"]
        if isinstance(value, bool):
            value = "true" if value else "false"
        params[f"{f['field']}__{f['op']}"] = str(value)
    if plan["sort"]:
        params["sort"] = plan["sort"]["field"]
        params["order"] = plan["sort"]["direction"]
    if plan["limit"] is not None:
        params["limit"] = str(plan["limit"])
    return params


def example_questions(schema):
    cols = schema["columns"]
    pk = schema.get("primary_key")
    examples = []
    for c in cols:
        if c["type"] == "string" and c.get("values"):
            examples.append(f"rows in {c['values'][0]}")
            break
    numeric = [c for c in cols
               if c["type"] in ("integer", "number") and c["name"] != pk]
    if numeric:
        examples.append(f"{numeric[0]['name']} over 10")
        examples.append(f"top 3 by {numeric[0]['name']}")
    for c in cols:
        if c["type"] == "date":
            examples.append(f"{c['name']} after 2025-01-01")
            break
    for c in cols:
        if c["type"] == "boolean":
            examples.append(f"only {c['name']}")
            break
    return examples


def help_text(schema):
    examples = example_questions(schema)
    if not examples:
        return "Mention a column name and a value or comparison."
    return "Try something like: " + "; ".join(f"'{e}'" for e in examples) + "."