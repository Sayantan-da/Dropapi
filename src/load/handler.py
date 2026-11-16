import csv
import io
import json
import logging
import os
import re
from datetime import date
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(os.environ["ITEMS_TABLE"])

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 100
MAX_KEY_LEN = 200
MAX_DISTINCT_VALUES = 30
MAX_VALUE_LEN = 40

DATASET_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
INT_RE = re.compile(r"-?(0|[1-9]\d*)")
DEC_RE = re.compile(r"-?(0|[1-9]\d*)\.\d+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ID_NAME_RE = re.compile(r"id$|(^|_)(key|code|sku|uuid)$", re.I)

# Which filter operators each column type supports (used by the API later)
OPS = {
    "integer": ["eq", "neq", "gt", "gte", "lt", "lte"],
    "number": ["eq", "neq", "gt", "gte", "lt", "lte"],
    "date": ["eq", "neq", "gt", "gte", "lt", "lte"],
    "boolean": ["eq", "neq"],
    "string": ["eq", "neq", "contains", "begins_with"],
}


class DatasetError(Exception):
    """A problem with the uploaded file that we can explain to the user."""


def lambda_handler(event, context):
    dataset_id = event["dataset_id"]
    try:
        result = load_dataset(dataset_id, event["bucket"], event["key"])
        logger.info(json.dumps({"event": "loaded", "dataset_id": dataset_id,
                                "rows": result["row_count"],
                                "primary_key": result["primary_key"]}))
        return {"ok": True, "error": "", **result}
    except DatasetError as exc:
        logger.warning(json.dumps({"event": "rejected", "dataset_id": dataset_id,
                                   "reason": str(exc)}))
        return {"ok": False, "error": str(exc), "row_count": 0}


def load_dataset(dataset_id, bucket, key):
    if not DATASET_ID_RE.fullmatch(dataset_id):
        raise DatasetError(
            "File name may only use letters, numbers, '-' and '_' (no spaces).")
    if not key.lower().endswith(".csv"):
        raise DatasetError("Only .csv files are supported right now.")

    size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if size == 0:
        raise DatasetError("The file is empty.")
    if size > MAX_BYTES:
        raise DatasetError("The file is larger than the 10 MB limit.")

    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DatasetError("The file is not valid UTF-8 text.")

    columns, rows = parse_csv(text)
    schema = infer_schema(columns, rows)
    types = {c["name"]: c["type"] for c in schema["columns"]}
    # Everything is validated before anything is deleted or written.
    replace_items(dataset_id, columns, rows, types, schema["primary_key"])
    return {
        "row_count": len(rows),
        "columns": columns,
        "schema": schema,
        "primary_key": schema["primary_key"],
    }


def detect_delimiter(text):
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(text):
    reader = csv.reader(io.StringIO(text), delimiter=detect_delimiter(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DatasetError("The file has no header row.")
    except csv.Error as exc:
        raise DatasetError(f"Could not read the CSV: {exc}")

    columns = [h.strip() for h in header]
    if len(columns) > MAX_COLUMNS:
        raise DatasetError(f"Too many columns (limit {MAX_COLUMNS}).")
    if any(c == "" for c in columns):
        raise DatasetError("The header row contains an empty column name.")
    if len(set(columns)) != len(columns):
        raise DatasetError("The header row contains duplicate column names.")

    rows = []
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(columns):
                raise DatasetError(
                    f"Line {reader.line_num} has {len(row)} columns "
                    f"but the header has {len(columns)}.")
            if len(rows) >= MAX_ROWS:
                raise DatasetError(f"Too many rows (limit {MAX_ROWS:,}).")
            rows.append({name: cell.strip() for name, cell in zip(columns, row)})
    except csv.Error as exc:
        raise DatasetError(f"Could not read the CSV: {exc}")

    if not rows:
        raise DatasetError("The file has a header but no data rows.")
    return columns, rows


def is_date(value):
    if not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def detect_type(values):
    """Pick one type for the whole column. Mixed columns fall back to string."""
    if not values:
        return "string"
    if all(v.lower() in ("true", "false") for v in values):
        return "boolean"
    if all(len(v) <= 15 and INT_RE.fullmatch(v) for v in values):
        return "integer"
    if all(len(v) <= 15 and (INT_RE.fullmatch(v) or DEC_RE.fullmatch(v))
           for v in values):
        return "number"
    if all(is_date(v) for v in values):
        return "date"
    return "string"


def convert(value, col_type):
    if col_type in ("integer", "number"):
        return Decimal(value)
    if col_type == "boolean":
        return value.lower() == "true"
    return value


def pick_primary_key(columns, rows):
    """A column that is filled in on every row and unique. Prefer id-like names."""
    n = len(rows)
    candidates = []
    for name in columns:
        values = [r[name] for r in rows]
        if (all(values) and len(set(values)) == n
                and max(len(v) for v in values) <= MAX_KEY_LEN):
            candidates.append(name)
    for name in candidates:
        if ID_NAME_RE.search(name):
            return name
    return candidates[0] if candidates else None


def infer_schema(columns, rows):
    n = len(rows)
    out = []
    for name in columns:
        values = [r[name] for r in rows if r[name] != ""]
        col_type = detect_type(values)
        distinct = set(values)
        col = {
            "name": name,
            "type": col_type,
            "nulls": n - len(values),
            "distinct": len(distinct),
            "ops": OPS[col_type],
        }
        # Small text columns: remember the values so /ask can match them later
        if (col_type == "string" and 0 < len(distinct) <= MAX_DISTINCT_VALUES
                and all(len(v) <= MAX_VALUE_LEN for v in distinct)):
            col["values"] = sorted(distinct)
        out.append(col)
    pk = pick_primary_key(columns, rows)
    return {"primary_key": pk or "", "columns": out}


def clear_existing(dataset_id):
    """Delete old rows so re-uploading the same file never creates duplicates."""
    kwargs = {
        "KeyConditionExpression": Key("dataset_id").eq(dataset_id),
        "ProjectionExpression": "dataset_id, row_key",
    }
    with table.batch_writer() as batch:
        while True:
            resp = table.query(**kwargs)
            for item in resp["Items"]:
                batch.delete_item(Key={"dataset_id": item["dataset_id"],
                                       "row_key": item["row_key"]})
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def replace_items(dataset_id, columns, rows, types, pk):
    clear_existing(dataset_id)
    with table.batch_writer() as batch:
        for i, raw in enumerate(rows, start=1):
            data = {name: convert(raw[name], types[name])
                    for name in columns if raw[name] != ""}
            row_key = raw[pk] if pk else f"{i:06d}"
            batch.put_item(Item={"dataset_id": dataset_id,
                                 "row_key": row_key,
                                 "data": data})