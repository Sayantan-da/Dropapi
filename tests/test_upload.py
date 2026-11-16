import importlib
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def upload_module(monkeypatch):
    upload_dir = Path(__file__).resolve().parents[1] / "src" / "upload"
    monkeypatch.syspath_prepend(str(upload_dir))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("DATASETS_TABLE", "datasets-test")
    monkeypatch.setenv("UPLOAD_BUCKET", "uploads-test")
    sys.modules.pop("upload", None)
    module = importlib.import_module("upload")
    module.datasets_table = Mock()
    module.s3 = Mock()
    yield module
    sys.modules.pop("upload", None)


def request(filename):
    return {"body": json.dumps({"filename": filename})}


@pytest.mark.parametrize(
    "filename",
    ["bad name.csv", "../bad.csv", "bad.txt", " bad.csv", "a" * 64 + ".csv"],
)
def test_invalid_filename_returns_400(upload_module, filename):
    response = upload_module.lambda_handler(request(filename), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]
    upload_module.datasets_table.get_item.assert_not_called()
    upload_module.s3.generate_presigned_post.assert_not_called()


def test_existing_dataset_returns_409(upload_module):
    upload_module.datasets_table.get_item.return_value = {
        "Item": {"dataset_id": "sales_2026"},
    }

    response = upload_module.lambda_handler(request("Sales_2026.csv"), None)

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error"] == (
        "Dataset 'sales_2026' already exists."
    )
    upload_module.datasets_table.get_item.assert_called_once_with(
        Key={"dataset_id": "sales_2026"},
        ConsistentRead=True,
    )
    upload_module.s3.generate_presigned_post.assert_not_called()


def test_valid_filename_returns_fixed_key_presigned_post(upload_module):
    upload_module.datasets_table.get_item.return_value = {}
    upload_module.s3.generate_presigned_post.return_value = {
        "url": "https://uploads-test.s3.ap-south-1.amazonaws.com/",
        "fields": {"key": "uploads/Sales_2026.csv", "Content-Type": "text/csv"},
    }

    response = upload_module.lambda_handler(request("Sales_2026.csv"), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["dataset_id"] == "sales_2026"
    assert body["max_bytes"] == 10 * 1024 * 1024
    upload_module.s3.generate_presigned_post.assert_called_once_with(
        Bucket="uploads-test",
        Key="uploads/Sales_2026.csv",
        Fields={"Content-Type": "text/csv"},
        Conditions=[
            {"Content-Type": "text/csv"},
            ["content-length-range", 1, 10 * 1024 * 1024],
        ],
        ExpiresIn=300,
    )
