from typing import List

from databricks.sdk.runtime import dbutils
from pyspark.sql import functions as f

from leap_utils.common import logger
from leap_utils.config import spark

logger.setup_applevel_logger()
LOGGER = logger.get_logger(__name__)

def archive_file(
    source_bucket: str,
    source_prefix: str,
    dest_bucket: str,
    dest_prefix: str,
    filename: str,
) -> bool:
    """
    Move a file from the source location to an archive folder.

    This function moves a file from a specified source path in an S3 bucket to a destination archive path 
    within a different S3 bucket. If the operation is successful, it logs the action; otherwise, it raises 
    an error.

    :param source_bucket: The name of the source bucket where the file currently resides.
    :type source_bucket: str
    :param source_prefix: The path within the source bucket, excluding the bucket name.
                          For example, 'erp/baan/'.
    :type source_prefix: str
    :param dest_bucket: The name of the destination bucket to which the file will be moved.
    :type dest_bucket: str
    :param dest_prefix: The path within the destination bucket, excluding the bucket name.
                         For example, 'archive/erp/baan/'.
    :type dest_prefix: str
    :param filename: The short name of the file to be archived (including file extension).
    :type filename: str

    :return: Returns `True` if the file was successfully moved, or `False` if an error occurred.
    :rtype: bool

    :raises OSError: If the file move operation fails, an error is raised.

    :example:
        >>> archive_file('source-bucket', 'erp/baan/', 'archive-bucket', 'archive/erp/baan/', 'file.csv')
        File file.csv archived to s3://archive-bucket/archive/erp/baan/file.csv
        True
    """
    source_path = f"s3://{source_bucket}/{source_prefix}{filename}"
    dest_path = f"s3://{dest_bucket}/{dest_prefix}{filename}"
    try:
        dbutils.fs.mv(source_path, dest_path)
    except Exception as e:
        LOGGER.error(f"Error archiving file, error is : {e}")
        raise OSError()

    LOGGER.info(f"File {filename} archived to {dest_path}\n--")
    return True


def get_last_schema_version(parent_folder_name: str) -> int:
    """
    Get the last schema version from the given parent folder information.

    This function looks into the specified folder and searches for folders matching the 
    pattern `SCHEMA_XX` where `XX` is a version number. It returns the highest version 
    found in the folder structure. If no version is found, the function returns `0`.

    :param parent_folder_name: The folder path where schema versioned folders are stored.
    :type parent_folder_name: str

    :return: The highest schema version found in the folder.
    :rtype: int

    :raises: If an error occurs while reading the folder or extracting versions, the function returns `0`.

    :example:
        >>> get_last_schema_version("s3://leap-s3-raw-sd-lat-dev/archive/apps/hr/")
        1
    """
    if not parent_folder_name:
        v = 0

    if not parent_folder_name.endswith("/"):
        parent_folder_name = parent_folder_name + "/"

    try:
        folder_content = dbutils.fs.ls(parent_folder_name)
        df = spark.createDataFrame(folder_content)
        df = df.withColumn(
            "version", f.regexp_extract("name", r"SCHEMA_(\d+)/$", 1).cast("int")
        )
        v = df.agg(f.max("version")).collect()[0][0]
        if not v:
            v = 0
    except:
        v = 0

    LOGGER.debug(f"schema_version_number is {v}")
    return v


def list_files_in_s3_bucket_with_prefix(
    bucket: str, prefix: str, recursive: bool = False
) -> List[str]:
    """
    Return a list of files in an S3 bucket that match a given prefix.

    This function lists all files in the specified S3 bucket and prefix path. Optionally, 
    it can search recursively within subfolders if the `recursive` parameter is set to `True`.

    :param bucket: The name of the S3 bucket where the files are stored.
    :type bucket: str
    :param prefix: The prefix (path) inside the bucket to filter files. For example, 'erp/baan/'.
    :type prefix: str
    :param recursive: A boolean flag indicating whether to search in subfolders as well.
                       Default is `False`, meaning only files directly under the given prefix will be listed.
    :type recursive: bool, optional
    :return: A list of file paths in the S3 bucket starting with the given prefix and including any subfolders if `recursive` is `True`.
    :rtype: List[str]

    :raises: Any error raised by `dbutils.fs.ls()` when accessing the S3 directory will result in an empty list being returned.

    :example:
        >>> list_files_in_s3_bucket_with_prefix('my-bucket', 'erp/baan/', recursive=True)
        ['s3://my-bucket/erp/baan/file1.csv', 's3://my-bucket/erp/baan/subfolder/file2.csv']

    """
    s3_folder_path = f"s3://{bucket}/{prefix}"
    files = []

    # need dbutils
    try:
        ls_result = dbutils.fs.ls(s3_folder_path)
    except:
        LOGGER.error(f"Error reading {s3_folder_path}")
        return []

    for file_info in ls_result:
        file_path = file_info.path
        if file_info.isFile():
            files.append(file_path)
        elif file_info.isDir() and recursive:
            files.extend(
                list_files_in_s3_bucket_with_prefix(
                    bucket, file_path.replace(f"s3://{bucket}/", ""), recursive
                )
            )

    return files