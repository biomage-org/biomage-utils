import json
import os
from pathlib import Path

import boto3
import click

from ..rds.run import run_rds_command
from ..utils.constants import (
    CELLSETS_BUCKET,
    DEFAULT_AWS_PROFILE,
    PROCESSED_FILES_BUCKET,
    RAW_FILES_BUCKET,
    SAMPLES_BUCKET,
    STAGING,
)

SAMPLES = "samples"
RAW_FILE = "raw_rds"
PROCESSED_FILE = "processed_rds"
CELLSETS = "cellsets"
SAMPLE_MAPPING = "sample_mapping"

SANDBOX_ID = "default"
REGION = "eu-west-1"
USER = "dev_role"

file_type_to_name_map = {
    "features10x": "features.tsv.gz",
    "matrix10x": "matrix.mtx.gz",
    "barcodes10x": "barcodes.tsv.gz",
}

DATA_LOCATION = os.getenv("BIOMAGE_DATA_PATH", "./data")


def _upload_file(bucket, s3_path, file_path, boto3_session):
    s3 = boto3_session.resource("s3")

    print(f"{file_path}, {bucket}, {s3_path}")
    s3.meta.client.upload_file(str(file_path), bucket, s3_path)


def _process_query_output(query_result):
    json_text = (
        query_result.replace("+", "")
        .split("\n", 2)[2]
        .replace("\n", "")
        .replace("(1 row)", "")
        .strip()
    )

    if not json_text:
        raise Exception("No data returned from query")

    return json.loads(json_text)


def _query_db(query, output_env, aws_profile):
    query = f"""psql -c "SELECT json_agg(q) FROM ( {query} ) AS q" """

    return _process_query_output(
        run_rds_command(
            query,
            SANDBOX_ID,
            output_env,
            USER,
            REGION,
            aws_profile,
            capture_output=True,
        )
    )


def _get_experiment_samples(experiment_id, output_env, aws_profile):
    query = f"""
        SELECT id as sample_id, name as sample_name \
            FROM sample WHERE experiment_id = '{experiment_id}'
    """

    return _query_db(query, output_env, aws_profile)


def _get_sample_files(sample_ids, output_env, aws_profile):
    query = f""" SELECT sample_id, s3_path, sample_file_type FROM sample_file \
            INNER JOIN sample_to_sample_file_map \
            ON sample_to_sample_file_map.sample_file_id = sample_file.id \
            WHERE sample_to_sample_file_map.sample_id IN ('{ "','".join(sample_ids) }')
    """

    return _query_db(query, output_env, aws_profile)


def _get_samples(experiment_id, output_env, aws_profile):
    print(f"Querying samples for {experiment_id}...")
    samples = _get_experiment_samples(experiment_id, output_env, aws_profile)

    sample_id_to_name = {}
    for sample in samples:
        sample_id_to_name[sample["sample_id"]] = sample["sample_name"]

    print(f"Querying sample files for {experiment_id}...")
    sample_ids = [entry["sample_id"] for entry in samples]
    sample_files = _get_sample_files(sample_ids, output_env, aws_profile)

    result = {}
    for sample_file in sample_files:
        sample_id = sample_file["sample_id"]
        sample_name = sample_id_to_name[sample_id]

        if not result.get(sample_name):
            result[sample_name] = []

        result[sample_name].append(
            {
                "sample_id": sample_id,
                "sample_name": sample_name,
                "s3_path": sample_file["s3_path"],
                "sample_file_name": file_type_to_name_map[
                    sample_file["sample_file_type"]
                ],
            }
        )

    return result


def _upload_samples(
    experiment_id,
    output_env,
    input_path,
    use_sample_id_as_name,
    boto3_session,
    aws_account_id,
    aws_profile,
):
    bucket = f"{SAMPLES_BUCKET}-{output_env}-{aws_account_id}"

    samples_list = _get_samples(experiment_id, output_env, aws_profile)
    num_samples = len(samples_list)

    print(f"\n{num_samples} samples found. uploading sample files...\n")

    for sample_idx, value in enumerate(samples_list.items()):
        sample_name, sample_files = value

        if use_sample_id_as_name:
            sample_name = sample_files[0]["sample_id"]

        num_files = len(sample_files)

        print(
            f"uploading files for sample {sample_name} (sample {sample_idx+1}/{num_samples})",
        )

        for file_idx, sample_file in enumerate(sample_files):
            s3_path = sample_file["s3_path"]

            file_name = sample_file["sample_file_name"]
            file_path = input_path / sample_name / file_name

            print(f"> uploading {s3_path} (file {file_idx+1}/{num_files})")

            s3client = boto3_session.client("s3")
            s3client.head_object(Bucket=bucket, Key=s3_path)
            _upload_file(bucket, s3_path, file_path, boto3_session)

        print(f"Sample {sample_name} uploaded.\n")

    click.echo(
        click.style(
            "All samples for the experiment have been uploaded.",
            fg="green",
        )
    )


def _upload_raw_rds_files(
    experiment_id,
    output_env,
    input_path,
    boto3_session,
    aws_account_id,
    aws_profile,
):

    bucket = f"{SAMPLES_BUCKET}-{output_env}-{aws_account_id}"

    sample_list = _get_experiment_samples(experiment_id, output_env, aws_profile)
    num_samples = len(sample_list)

    print(f"\n{num_samples} samples found. Uploading raw rds files...\n")

    bucket = f"{RAW_FILES_BUCKET}-{output_env}-{aws_account_id}"
    end_message = "Raw RDS files have been uploaded."

    for sample_idx, sample in enumerate(sample_list):
        sample_id = sample["sample_id"]
        sample_name = sample["sample_name"]

        s3_path = f"{experiment_id}/{sample_id}/r.rds"

        file_path = input_path / "raw" / f"{sample_name}.rds"

        print(f"uploading {sample_name} ({sample_idx+1}/{num_samples})")

        _upload_file(bucket, s3_path, file_path, boto3_session)

        print(f"Sample {sample_name} uploaded.\n")

    print(end_message)


def _upload_processed_rds_file(
    experiment_id,
    output_env,
    input_path,
    boto3_session,
    aws_account_id,
):

    file_name = "processed_r.rds"
    bucket = f"{PROCESSED_FILES_BUCKET}-{output_env}-{aws_account_id}"
    end_message = "Processed RDS files have been uploaded."

    key = f"{experiment_id}/r.rds"
    file_path = input_path / file_name

    _upload_file(bucket, key, file_path, boto3_session)

    print(f"RDS file saved to {file_path}")
    click.echo(click.style(f"{end_message}", fg="green"))


def _upload_cellsets(
    experiment_id, output_env, input_path, boto3_session, aws_account_id
):
    FILE_NAME = "cellsets.json"

    bucket = f"{CELLSETS_BUCKET}-{output_env}-{aws_account_id}"
    key = experiment_id
    file_path = input_path / FILE_NAME
    _upload_file(bucket, key, file_path, boto3_session)
    click.echo(
        click.style(f"Cellsets file have been uploaded to {experiment_id}.", fg="green")
    )


@click.command()
@click.option(
    "-e",
    "--experiment_id",
    required=True,
    help="Experiment ID to be copied.",
)
@click.option(
    "-o",
    "--output_env",
    required=True,
    default=STAGING,
    show_default=True,
    help="Output environment to upload the data to.",
)
@click.option(
    "-i",
    "--input_path",
    required=False,
    default=".",
    show_default=True,
    help="Input path. By default points to BIOMAGE_DATA_PATH/experiment_id.",
)
@click.option(
    "-a",
    "--all",
    required=False,
    is_flag=True,
    default=False,
    show_default=True,
    help="upload all files for the experiment.",
)
@click.option(
    "-f",
    "--files",
    multiple=True,
    required=True,
    show_default=True,
    help=(
        "Files to upload. You can also upload cellsets (-f cellsets), raw RDS "
        "(-f raw_rds) and processed RDS (-f processed_rds)."
    ),
)
@click.option(
    "-p",
    "--aws_profile",
    required=False,
    default=DEFAULT_AWS_PROFILE,
    show_default=True,
    help="The name of the profile stored in ~/.aws/credentials to use.",
)
def upload(experiment_id, output_env, input_path, files, all, aws_profile):
    """
    Uploads the files in input_path into the specified experiment_id and environment.\n
    It requires an open tunnel to the desired environment to fetch data from SQL:
    `biomage rds tunnel -i staging`

    E.g.:
    biomage experiment upload -o staging -e 2093e95fd17372fb558b81b9142f230e
    -f samples -f cellsets -o output/folder
    """

    boto3_session = boto3.Session(profile_name=aws_profile)
    aws_account_id = boto3_session.client("sts").get_caller_identity().get("Account")

    # Set output path
    # By default add experiment_id to the output path
    if input_path == DATA_LOCATION:
        input_path = Path(DATA_LOCATION)
    else:
        input_path = Path(os.getcwd()) / input_path

    print("Uploading files from: ", str(input_path))

    selected_files = []
    if all:
        selected_files = [CELLSETS, RAW_FILE, PROCESSED_FILE]
    else:
        selected_files = list(files)

    print(f"files: {files}")
    for file in selected_files:
        if file == SAMPLES:
            print("\n== Uploading sample files is not supported")

        elif file == RAW_FILE:
            print("\n== uploading raw RDS file")
            _upload_raw_rds_files(
                experiment_id,
                output_env,
                input_path,
                boto3_session,
                aws_account_id,
                aws_profile,
            )

        elif file == PROCESSED_FILE:
            print("\n== uploading processed RDS file")
            _upload_processed_rds_file(
                experiment_id,
                output_env,
                input_path,
                boto3_session,
                aws_account_id,
            )

        elif file == CELLSETS:
            print("\n== upload cellsets file")
            _upload_cellsets(
                experiment_id, output_env, input_path, boto3_session, aws_account_id
            )