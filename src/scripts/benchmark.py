import csv, io, random, sys, time
import boto3
from boto3.dynamodb.conditions import Key

REGION = "ap-south-1"
rows = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

cf = boto3.client("cloudformation", region_name=REGION)
outs = {o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName="dropapi")["Stacks"][0]["Outputs"]}
bucket = outs["UploadBucketName"]
db = boto3.resource("dynamodb", region_name=REGION)
datasets, items = db.Table(outs["DatasetsTableName"]), db.Table(outs["ItemsTableName"])
s3 = boto3.client("s3", region_name=REGION)

name = f"bench_{rows}_{int(time.time())}"
buf = io.StringIO()
w = csv.writer(buf)
w.writerow(["order_id", "customer", "city", "product", "quantity", "price", "order_date"])
for i in range(1, rows + 1):
    w.writerow([i, f"Customer {i}", random.choice(["Kolkata", "Mumbai", "Delhi", "Pune"]),
                random.choice(["Notebook", "Pen Set", "Backpack"]), random.randint(1, 9),
                round(random.uniform(10, 900), 2), f"2026-11-{random.randint(1, 28):02d}"])
data = buf.getvalue().encode()

start = time.time()
s3.put_object(Bucket=bucket, Key=f"uploads/{name}.csv", Body=data)
item = None
while time.time() - start < 600:
    item = datasets.get_item(Key={"dataset_id": name}).get("Item")
    if item and item.get("status") in ("READY", "FAILED"):
        break
    time.sleep(0.5)
secs = time.time() - start
print(f"rows={rows} size={len(data)/1024:.0f}KB status={item and item.get('status')} "
      f"seconds={secs:.1f} error={item and item.get('error')}")

if "keep" not in sys.argv:   # remove the benchmark data so it doesn't clutter your demo page
    kw = {"KeyConditionExpression": Key("dataset_id").eq(name),
          "ProjectionExpression": "dataset_id, row_key"}
    with items.batch_writer() as b:
        while True:
            r = items.query(**kw)
            for it in r["Items"]:
                b.delete_item(Key={"dataset_id": it["dataset_id"], "row_key": it["row_key"]})
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    datasets.delete_item(Key={"dataset_id": name})
    s3.delete_object(Bucket=bucket, Key=f"uploads/{name}.csv")