import base64
import json
import logging
import os
import re
import time
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import boto3
import nlq

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
datasets_table = dynamodb.Table(os.environ["DATASETS_TABLE"])
items_table = dynamodb.Table(os.environ["ITEMS_TABLE"])

DATASET_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
INT_RE = re.compile(r"-?(0|[1-9]\d*)")
DEC_RE = re.compile(r"-?(0|[1-9]\d*)\.\d+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "begins_with"}
COMPARE = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
CONTROL = {"limit", "cursor", "sort", "order"}
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_FILTERS = 8
MAX_VALUE_LEN = 200


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- helpers ----------

def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Cannot serialize {type(value)}")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body, default=json_default),
    }


def is_valid_date(value):
    if not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def encode_cursor(row_key):
    return base64.urlsafe_b64encode(row_key.encode()).decode().rstrip("=")


def decode_cursor(cursor):
    if len(cursor) > 400:
        raise ApiError(400, "Invalid cursor.")
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except Exception:
        raise ApiError(400, "Invalid cursor.")


# ---------- datasets ----------

def get_dataset(dataset_id):
    if not DATASET_ID_RE.fullmatch(dataset_id):
        raise ApiError(400, "Invalid dataset id.")
    item = datasets_table.get_item(Key={"dataset_id": dataset_id}).get("Item")
    if not item:
        raise ApiError(404, f"Dataset '{dataset_id}' not found.")
    return item


def get_ready(dataset_id):
    dataset = get_dataset(dataset_id)
    status = dataset.get("status")
    if status != "READY":
        detail = f" Reason: {dataset['error']}" if dataset.get("error") else ""
        raise ApiError(409, f"Dataset '{dataset_id}' is {status}, not READY.{detail}")
    return dataset, json.loads(dataset["schema_json"])


def list_datasets():
    kwargs = {
        "ProjectionExpression": "dataset_id, #st, row_count, primary_key, finished_at, #er",
        "ExpressionAttributeNames": {"#st": "status", "#er": "error"},
    }
    found = []
    while True:
        resp = datasets_table.scan(**kwargs)
        found.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    found.sort(key=lambda d: d["dataset_id"])
    rows = [{
        "dataset_id": d["dataset_id"],
        "status": d.get("status"),
        "row_count": d.get("row_count"),
        "primary_key": d.get("primary_key") or None,
        "finished_at": d.get("finished_at"),
        "error": d.get("error"),
    } for d in found]
    return {"count": len(rows), "datasets": rows}


def dataset_detail(dataset_id):
    dataset = get_dataset(dataset_id)
    detail = {
        "dataset_id": dataset_id,
        "status": dataset.get("status"),
        "row_count": dataset.get("row_count"),
        "primary_key": dataset.get("primary_key") or None,
        "finished_at": dataset.get("finished_at"),
        "error": dataset.get("error"),
    }
    if dataset.get("schema_json"):
        detail["columns"] = json.loads(dataset["schema_json"])["columns"]
        detail["try_it"] = f"/{dataset_id}/items?limit=5"
    return detail


# ---------- filtering ----------

def coerce(col, op, raw):
    name, col_type = col["name"], col["type"]
    if op not in col["ops"]:
        raise ApiError(400, f"Operator '{op}' is not supported for {col_type} "
                            f"column '{name}'. Allowed: {', '.join(col['ops'])}.")
    value = raw.strip()
    if value == "":
        raise ApiError(400, f"The value for '{name}' is empty.")
    if len(value) > MAX_VALUE_LEN:
        raise ApiError(400, f"The value for '{name}' is too long.")
    if col_type == "integer":
        if len(value) > 15 or not INT_RE.fullmatch(value):
            raise ApiError(400, f"'{name}' expects a whole number, got '{value}'.")
        return Decimal(value)
    if col_type == "number":
        if len(value) > 15 or not (INT_RE.fullmatch(value) or DEC_RE.fullmatch(value)):
            raise ApiError(400, f"'{name}' expects a number, got '{value}'.")
        return Decimal(value)
    if col_type == "boolean":
        if value.lower() not in ("true", "false"):
            raise ApiError(400, f"'{name}' expects true or false, got '{value}'.")
        return value.lower() == "true"
    if col_type == "date":
        if not is_valid_date(value):
            raise ApiError(400, f"'{name}' expects a date like 2026-11-14, got '{value}'.")
        return value
    # string: match known values case-insensitively for exact comparisons
    if op in ("eq", "neq") and col.get("values"):
        by_lower = {v.lower(): v for v in col["values"]}
        return by_lower.get(value.lower(), value)
    return value


def parse_params(params, schema):
    cols = {c["name"]: c for c in schema["columns"]}
    filters, control = [], {}
    for key, raw in params.items():
        field, op, explicit = key, "eq", False
        if "__" in key:
            left, _, right = key.rpartition("__")
            if right in OPS:
                field, op, explicit = left, right, True
        if not explicit and key in CONTROL:
            control[key] = raw
            continue
        if field not in cols:
            raise ApiError(400, f"Unknown field '{field}'. "
                                f"Valid fields: {', '.join(cols)}.")
        filters.append((field, op, coerce(cols[field], op, raw)))
    if len(filters) > MAX_FILTERS:
        raise ApiError(400, f"Too many filters (limit {MAX_FILTERS}).")
    return cols, filters, control


def build_filter(filters):
    names, values, parts = {}, {}, []
    if filters:
        names["#d"] = "data"
    for i, (field, op, value) in enumerate(filters):
        names[f"#f{i}"] = field
        values[f":v{i}"] = value
        ref = f"#d.#f{i}"
        if op in COMPARE:
            parts.append(f"{ref} {COMPARE[op]} :v{i}")
        else:
            parts.append(f"{op}({ref}, :v{i})")
    return " AND ".join(parts), names, values


def parse_limit(raw):
    if raw is None:
        return DEFAULT_LIMIT
    if not re.fullmatch(r"\d{1,4}", raw) or not 1 <= int(raw) <= MAX_LIMIT:
        raise ApiError(400, f"limit must be a whole number from 1 to {MAX_LIMIT}.")
    return int(raw)


def shape(item, schema):
    data = item.get("data", {})
    return {c["name"]: data.get(c["name"]) for c in schema["columns"]}


def query_items(dataset_id, params):
    _, schema = get_ready(dataset_id)
    cols, filters, control = parse_params(params, schema)
    limit = parse_limit(control.get("limit"))

    sort = control.get("sort")
    order = control.get("order", "asc").lower()
    if order not in ("asc", "desc"):
        raise ApiError(400, "order must be 'asc' or 'desc'.")
    if sort and sort not in cols:
        raise ApiError(400, f"Cannot sort by unknown field '{sort}'. "
                            f"Valid fields: {', '.join(cols)}.")
    if sort and "cursor" in control:
        raise ApiError(400, "cursor cannot be combined with sort.")

    filter_expr, names, values = build_filter(filters)
    names["#ds"] = "dataset_id"
    values[":ds"] = dataset_id
    kwargs = {
        "KeyConditionExpression": "#ds = :ds",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if filter_expr:
        kwargs["FilterExpression"] = filter_expr
    if control.get("cursor"):
        kwargs["ExclusiveStartKey"] = {
            "dataset_id": dataset_id,
            "row_key": decode_cursor(control["cursor"]),
        }

    collect_all = bool(sort)
    matched = []
    while True:
        resp = items_table.query(**kwargs)
        matched.extend(resp["Items"])
        if not collect_all and len(matched) > limit:
            break
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    next_cursor = None
    if collect_all:
        present = [i for i in matched if sort in i.get("data", {})]
        missing = [i for i in matched if sort not in i.get("data", {})]
        present.sort(key=lambda i: i["data"][sort], reverse=(order == "desc"))
        page = (present + missing)[:limit]
    else:
        page = matched[:limit]
        if len(matched) > limit:
            next_cursor = encode_cursor(page[-1]["row_key"])

    return {
        "dataset": dataset_id,
        "count": len(page),
        "items": [shape(i, schema) for i in page],
        "next_cursor": next_cursor,
    }


def get_item(dataset_id, key):
    _, schema = get_ready(dataset_id)
    if not key or len(key) > MAX_VALUE_LEN:
        raise ApiError(400, "Invalid key.")
    item = items_table.get_item(
        Key={"dataset_id": dataset_id, "row_key": key}).get("Item")
    if not item:
        raise ApiError(404, f"No row with key '{key}' in '{dataset_id}'.")
    return shape(item, schema)


# ---------- OpenAPI ----------

TYPE_SCHEMA = {
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "string": {"type": "string"},
}


def build_openapi(dataset_id, dataset, schema, host):
    params, props = [], {}
    for col in schema["columns"]:
        name, col_type = col["name"], col["type"]
        base = dict(TYPE_SCHEMA[col_type])
        props[name] = {**base, "nullable": True}

        exact = dict(base)
        if col.get("values"):
            exact["enum"] = col["values"]
        params.append({"name": name, "in": "query", "required": False,
                       "description": f"Exact match on {col_type} column.",
                       "schema": exact})
        if col_type in ("integer", "number", "date"):
            for op, label in (("gte", "at least"), ("lte", "at most")):
                params.append({"name": f"{name}__{op}", "in": "query",
                               "required": False,
                               "description": f"{name} is {label} this value.",
                               "schema": dict(base)})
        elif col_type == "string":
            params.append({"name": f"{name}__contains", "in": "query",
                           "required": False,
                           "description": f"{name} contains this text (case-sensitive).",
                           "schema": {"type": "string"}})

    names = [c["name"] for c in schema["columns"]]
    params += [
        {"name": "limit", "in": "query", "required": False,
         "description": f"Rows to return (1-{MAX_LIMIT}).",
         "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT}},
        {"name": "sort", "in": "query", "required": False,
         "description": "Sort by this column (cannot be combined with cursor).",
         "schema": {"type": "string", "enum": names}},
        {"name": "order", "in": "query", "required": False,
         "schema": {"type": "string", "enum": ["asc", "desc"], "default": "asc"}},
        {"name": "cursor", "in": "query", "required": False,
         "description": "Pass next_cursor from the previous response.",
         "schema": {"type": "string"}},
    ]

    pk = dataset.get("primary_key") or None
    error = {"description": "Error",
             "content": {"application/json": {"schema": {
                 "type": "object", "properties": {"error": {"type": "string"}}}}}}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"{dataset_id} API",
            "version": "1.0.0",
            "description": (
                f"Auto-generated by DropAPI from {dataset.get('row_count')} rows. "
                "Filter with ?column=value (exact match) or ?column__op=value. "
                "Operators: eq, neq, gt, gte, lt, lte (numbers and dates), "
                "contains, begins_with (text). Only the common filters are "
                "listed below, but every operator the column type allows works."
            ),
        },
        "servers": [{"url": f"https://{host}"}],
        "paths": {
            f"/{dataset_id}/items": {"get": {
                "summary": "List or filter rows",
                "parameters": params,
                "responses": {
                    "200": {"description": "Matching rows", "content": {
                        "application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "dataset": {"type": "string"},
                                "count": {"type": "integer"},
                                "items": {"type": "array", "items": {
                                    "type": "object", "properties": props}},
                                "next_cursor": {"type": "string", "nullable": True},
                            }}}}},
                    "400": error, "409": error,
                }}},
            f"/{dataset_id}/ask": {"get": {
                "summary": "Ask a question in plain English",
                "description": ("Examples: 'rows in Kolkata', 'age over 30', "
                                "'top 3 by price'. The response shows the "
                                "exact filter that was generated."),
                "parameters": [{"name": "q", "in": "query", "required": True,
                                "description": "Your question in plain English.",
                                "schema": {"type": "string", "maxLength": 300}}],
                "responses": {
                    "200": {"description": "The generated filter and the matching rows"},
                    "400": error,
                }}},
            f"/{dataset_id}/items/{{key}}": {"get": {
                "summary": "Get one row by key",
                "parameters": [{"name": "key", "in": "path", "required": True,
                                "description": (f"Value of '{pk}'." if pk
                                                else "Row number, e.g. 000001."),
                                "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "The row", "content": {
                        "application/json": {"schema": {
                            "type": "object", "properties": props}}}},
                    "404": error,
                }}},
        },
    }


def openapi(dataset_id, event):
    dataset, schema = get_ready(dataset_id)
    host = event["requestContext"]["domainName"]
    return build_openapi(dataset_id, dataset, schema, host)


# ---------- ask (plain English) ----------

def ask_endpoint(dataset_id, params):
    _, schema = get_ready(dataset_id)
    question = (params.get("q") or "").strip()
    if not question:
        raise ApiError(400, "Add a question with ?q=... " + nlq.help_text(schema))
    if len(question) > nlq.MAX_QUESTION_LEN:
        raise ApiError(400, f"Question is too long (limit "
                            f"{nlq.MAX_QUESTION_LEN} characters).")
    plan = nlq.parse_question(question, schema)
    if not plan["filters"] and not plan["sort"] and plan["limit"] is None:
        raise ApiError(400, "I couldn't turn that into a filter. "
                            + nlq.help_text(schema))
    result = query_items(dataset_id, nlq.to_params(plan))
    return {
        "question": question,
        "generated_filter": {
            "filters": plan["filters"],
            "sort": plan["sort"],
            "limit": plan["limit"] or DEFAULT_LIMIT,
        },
        "notes": plan["notes"],
        "count": result["count"],
        "rows": result["items"],
    }


# ---------- routing ----------

def info():
    return {
        "service": "DropAPI",
        "endpoints": [
            "GET /datasets",
            "GET /datasets/{id}",
            "POST /upload-url",
            "GET /{id}/items?column=value&limit=10&sort=column&order=desc",
            "GET /{id}/items/{key}",
            "GET /{id}/ask?q=customers in Kolkata older than 30",
            "GET /{id}/openapi.json",
        ],
    }


def route(event):
    if event["requestContext"]["http"]["method"] != "GET":
        raise ApiError(405, "Only GET is supported.")
    segs = [unquote(s) for s in event.get("rawPath", "/").strip("/").split("/") if s]
    params = event.get("queryStringParameters") or {}

    if not segs:
        return info()
    if segs[0] == "datasets":
        if len(segs) == 1:
            return list_datasets()
        if len(segs) == 2:
            return dataset_detail(segs[1])
        raise ApiError(404, "Route not found.")
    dataset_id = segs[0]
    if len(segs) == 2 and segs[1] == "items":
        return query_items(dataset_id, params)
    if len(segs) == 3 and segs[1] == "items":
        return get_item(dataset_id, segs[2])
    if len(segs) == 2 and segs[1] == "ask":
        return ask_endpoint(dataset_id, params)
    if len(segs) == 2 and segs[1] == "openapi.json":
        return openapi(dataset_id, event)
    raise ApiError(404, "Route not found.")


def lambda_handler(event, context):
    if event["requestContext"]["http"]["method"] == "OPTIONS":
        return {"statusCode": 204,
                "headers": {"Access-Control-Allow-Origin": "*",
                            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type,X-Api-Key"},
                "body": ""}

    started = time.time()
    status = 200
    try:
        body = route(event)
    except ApiError as exc:
        status, body = exc.status, {"error": exc.message}
    except Exception:
        logger.exception("Unhandled error")
        status, body = 500, {"error": "Internal server error."}
    logger.info(json.dumps({
        "event": "request",
        "request_id": event.get("requestContext", {}).get("requestId"),
        "path": event.get("rawPath"),
        "status": status,
        "ms": int((time.time() - started) * 1000),
    }))
    return respond(status, body)