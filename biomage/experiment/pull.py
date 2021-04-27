import boto3
import click

from .utils import (
    PULL,
    Summary,
    download_S3_json,
    download_S3_rds,
    is_modified,
    load_cfg_file,
    save_cfg_file,
)

CELLSETS_FILE = "mock_cell_sets.json"
PLOTS_TABLES_FILE = "mock_plots_tables.json"
EXPERIMENTS_FILE = "mock_experiment.json"
SAMPLES_FILE = "mock_samples.json"
RDS_FILE = "r.rds"

SAMPLES_TABLE = "samples"
EXPERIMENTS_TABLE = "experiments"


def download_S3_obj(s3_obj, key, filepath):
    if RDS_FILE in filepath:
        download_S3_rds(s3_obj, key, filepath)
    elif CELLSETS_FILE in filepath:
        download_S3_json(s3_obj, key, filepath)
    else:
        raise ValueError(f"unexpected file: {filepath}")


def download_if_modified(bucket, key, filepath):
    s3 = boto3.resource("s3")
    s3_obj = s3.Object(bucket, key)

    if is_modified(s3_obj, filepath):
        click.echo(
            f"Local file for key {key} last modified date "
            "differs from S3 version.\n Updating local copy"
        )

        download_S3_obj(s3_obj, key, filepath)
        Summary.add_changed_file(filepath)


def remove_key(dic, k):
    if k in dic:
        del dic[k]
    for val in dic.values():
        if isinstance(val, dict):
            remove_key(val, k)
    return dic


def update_config_if_needed(filepath, table_name, experiment_id):
    """
    Filepath: experiment_id/filename
    """
    dynamodb = boto3.resource("dynamodb")

    local_cfg, found = load_cfg_file(filepath)
    remote_cfg = dynamodb.Table(table_name).get_item(
        Key={"experimentId": experiment_id}
    )["Item"]

    # the "pipeline" field in experiment config has information about
    # the production pipeline Arn causing a crash with ExecutionDoesNotExist
    # locally in the API. This solution is not ideal as it will fail
    # if the field name changes or more tightly coupled info is added
    # TODO: make api handle not found cases, or ignore keys in development env
    if "experiment" in table_name:
        remote_cfg = remove_key(remote_cfg, "pipeline")

    # if the local config was not found or it's different from the remote => update
    if not found or local_cfg != remote_cfg:
        save_cfg_file(remote_cfg, filepath)
        Summary.add_changed_file(filepath)


def update_configs(experiment_id, origin):
    # config pairs like: (local file name, remote table name)
    configs = [
        (EXPERIMENTS_FILE, EXPERIMENTS_TABLE),
        (SAMPLES_FILE, SAMPLES_TABLE),
    ]

    for file_name, table_name in configs:
        file_path = f"{experiment_id}/{file_name}"
        table_name = f"{table_name}-{origin}"
        update_config_if_needed(file_path, table_name, experiment_id)

    # plots and tables config has key issues (references that do no
    # exist locally), for now just create an empty json
    empty_plots_tables = {"records": []}
    filepath = f"{experiment_id}/{PLOTS_TABLES_FILE}"
    save_cfg_file(empty_plots_tables, filepath)
    Summary.add_changed_file(filepath)


@click.command()
@click.argument(
    "origin",
    default="production",
)
@click.argument(
    "experiment_id",
    default="e52b39624588791a7889e39c617f669e",
    required=False,
)
def pull(experiment_id, origin):
    """
    Downloads experiment data and config files from a given environment.\n

    [EXPERIMENT_ID]: experiment to get (default: e52b39624588791a7889e39c617f669e)

    [ORIGIN]: environmnent to fetch the data from (default: production)

    Works only with r.rds datasets.\n
    """

    Summary.set_command(cmd=PULL, origin=origin, experiment_id=experiment_id)

    bucket = f"biomage-source-{origin}"
    file = f"{experiment_id}/{RDS_FILE}"
    dst_file = file + ".gz"
    download_if_modified(bucket=bucket, key=file, filepath=dst_file)

    bucket = f"cell-sets-{origin}"
    dst_file = f"{experiment_id}/{CELLSETS_FILE}"

    # the name of the cell sets file in S3 is just the experiment ID
    download_if_modified(bucket=bucket, key=experiment_id, filepath=dst_file)

    update_configs(experiment_id, origin)

    Summary.report_changes()