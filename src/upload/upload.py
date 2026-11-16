import base64
import json
import logging
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_BYTES = 10 * 1024 * 1024
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.csv")

s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
datasets_table = boto3.resource("dynamodb").Table(os.environ["DATASETS_TABLE"])
upload_bucket = os.environ["UPLOAD_BUCKET"]


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def parse_body(event):
    body = event.get("body")
    if not isinstance(body, str):
        raise RequestError(400, "Request body must be a JSON object.")
    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise RequestError(400, "Request body must be valid JSON.")
    if not isinstance(payload, dict):
        raise RequestError(400, "Request body must be a JSON object.")
    return payload


def validate_filename(filename):
    if not isinstance(filename, str) or not FILENAME_RE.fullmatch(filename):
        raise RequestError(
            400,
            "filename must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}.csv.",
        )
    dataset_id = filename[:-4].lower()
    if len(dataset_id) > 63:
        raise RequestError(
            400,
            "The filename must have a basename of at most 63 characters.",
        )
    return dataset_id


def lambda_handler(event, context):
    try:
        payload = parse_body(event)
        dataset_id = validate_filename(payload.get("filename"))
    except RequestError as exc:
        return respond(exc.status, {"error": exc.message})

    try:
        existing = datasets_table.get_item(
            Key={"dataset_id": dataset_id},
            ConsistentRead=True,
        ).get("Item")
        if existing:
            return respond(409, {"error": f"Dataset '{dataset_id}' already exists."})

        filename = payload["filename"]
        result = s3.generate_presigned_post(
            Bucket=upload_bucket,
            Key=f"uploads/{filename}",
            Fields={"Content-Type": "text/csv"},
            Conditions=[
                {"Content-Type": "text/csv"},
                ["content-length-range", 1, MAX_BYTES],
            ],
            ExpiresIn=300,
        )
    except (BotoCoreError, ClientError):
        logger.exception("Could not create CSV upload request")
        return respond(500, {"error": "Could not create an upload request."})

    return respond(200, {
        "url": result["url"],
        "fields": result["fields"],
        "dataset_id": dataset_id,
        "max_bytes": MAX_BYTES,
    })
