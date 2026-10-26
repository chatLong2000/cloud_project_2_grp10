import os
import json
import boto3
from botocore.exceptions import ClientError

# Initialisation du client en dehors du handler pour réutiliser la connexion TCP (Warm Start)
REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_NAME = os.environ.get("TIMESTREAM_DB_NAME", "SdatawayIoTDB")
TABLE_NAME = os.environ.get("TIMESTREAM_TABLE_NAME", "telemetry")

write_client = boto3.client("timestream-write", region_name=REGION)


def lambda_handler(event, context):
    """
    Reçoit un batch JSON depuis AWS IoT Core (topic: coffee_G10/+/batch)
    et l'insère dans Amazon Timestream en mode Multi-Measure optimisé.
    """
    readings = event.get("readings", [])
    if not readings:
        print("⚠️ Aucun 'readings' trouvé dans l'événement reçu.")
        return {"statusCode": 400, "body": "No readings in payload"}

    edge_id = str(event.get("edge_id", "unknown-edge"))
    root_device_id = str(event.get("device_id", readings[0].get("device_id", "unknown-device")))
    sent_at = str(event.get("sent_at", ""))

    # 1. Attributs partagés par toutes les mesures du batch (Optimisation Timestream)
    dimensions = [
        {"Name": "device_id", "Value": root_device_id},
        {"Name": "edge_id", "Value": edge_id}
    ]
    
    common_attributes = {
        "Dimensions": dimensions,
        "MeasureName": "telemetry_metrics",
        "MeasureValueType": "MULTI",
        "TimeUnit": "MILLISECONDS"
    }

    # 2. Construction de la liste des enregistrements individuels
    records = []
    for r in readings:
        record = {
            "Time": str(r["timestamp"]),
            "MeasureValues": [
                {"Name": "uptime_ms", "Value": str(r["uptime_ms"]), "Type": "BIGINT"},
                {"Name": "temperature_c", "Value": str(r["temperature_c"]), "Type": "DOUBLE"},
                {"Name": "power_consumption_mw", "Value": str(r["power_consumption_mw"]), "Type": "DOUBLE"},
                {"Name": "heap_free_bytes", "Value": str(r["heap_free_bytes"]), "Type": "BIGINT"},
                {"Name": "device_status", "Value": str(r["device_status"]), "Type": "VARCHAR"},
                {"Name": "error_code", "Value": str(r["error_code"]), "Type": "BIGINT"}
            ]
        }
        records.append(record)

    # 3. Envoi par tranches de 100 records maximum (limite de l'API Timestream WriteRecords)
    BATCH_LIMIT = 100
    inserted_count = 0

    for i in range(0, len(records), BATCH_LIMIT):
        chunk = records[i:i + BATCH_LIMIT]
        try:
            write_client.write_records(
                DatabaseName=DB_NAME,
                TableName=TABLE_NAME,
                CommonAttributes=common_attributes,
                Records=chunk
            )
            inserted_count += len(chunk)
        except write_client.exceptions.RejectedRecordsException as err:
            print(f"❌ Enregistrements rejetés par Timestream pour {root_device_id} :")
            for rr in err.response.get("RejectedRecords", []):
                print(f"   Index {rr['RecordIndex']} : {rr['Reason']}")
            raise err
        except ClientError as err:
            print(f"❌ Erreur AWS Timestream : {err}")
            raise err

    print(f"✅ Batch inséré : {inserted_count} mesures pour '{root_device_id}' (via '{edge_id}', sent_at={sent_at})")
    return {
        "statusCode": 200,
        "device_id": root_device_id,
        "inserted_records": inserted_count
    }