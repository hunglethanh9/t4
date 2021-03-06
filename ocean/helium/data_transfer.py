import json
import os
import pathlib
from threading import Lock, get_ident

import boto3
from boto3.s3.transfer import S3Transfer, TransferConfig, create_transfer_manager
from s3transfer.subscribers import BaseSubscriber
from tqdm.autonotebook import tqdm

from .util import HeliumException, split_path


HELIUM_METADATA = 'helium'

s3_client = boto3.client('s3')
s3_manager = create_transfer_manager(s3_client, TransferConfig())


class SizeCallback(BaseSubscriber):
    def __init__(self, size):
        self.size = size

    def on_queued(self, future, **kwargs):
        future.meta.provide_transfer_size(self.size)


class ProgressCallback(BaseSubscriber):
    def __init__(self, progress):
        self._progress = progress
        self._lock = Lock()

    def on_progress(self, future, bytes_transferred, **kwargs):
        with self._lock:
            self._progress.update(bytes_transferred)


def _parse_metadata(resp):
    return json.loads(resp['Metadata'].get(HELIUM_METADATA, '{}'))


def _download_single_file(bucket, key, dest_path, version=None):
    params = dict(Bucket=bucket, Key=key)
    if version is not None:
        params.update(dict(VersionId=version))
    resp = s3_client.head_object(**params)
    size = resp['ContentLength']
    # meta = _parse_metadata(resp)
    extra = dict(VersionId=version) if version is not None else {}
    
    if dest_path.endswith('/'):
        dest_path += pathlib.PurePosixPath(key).name

    if pathlib.Path(dest_path).is_reserved():
        raise ValueError("Cannot download %r: reserved file name" % dest_path)

    with tqdm(total=size, unit='B', unit_scale=True) as progress:
        future = s3_manager.download(
            bucket, key, dest_path, subscribers=[SizeCallback(size), ProgressCallback(progress)],
            extra_args=extra
        )
        future.result()


def _download_dir(bucket, prefix, dest_path):
    if not dest_path.endswith('/'):
        raise ValueError("Destination path must end in /")

    dest_dir = pathlib.Path(dest_path)

    total_size = 0
    tuples_list = []

    continuation_token = None
    kwargs = dict()

    while True:
        resp = s3_client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            **kwargs
        )

        for item in resp.get('Contents', []):
            key = item['Key']
            size = item['Size']
            total_size += size

            rel_key = key[len(prefix):]
            dest_file = dest_dir / rel_key

            # Make sure it doesn't contain '..' or anything like that
            try:
                dest_file.resolve().relative_to(dest_dir.resolve())
            except ValueError:
                raise ValueError("Cannot download %r: outside of destination directory" % dest_file)

            if dest_file.is_reserved():
                raise ValueError("Cannot download %r: reserved file name" % dest_file)

            tuples_list.append((key, dest_file, size))

        if not resp['IsTruncated']:
            break

        kwargs = dict(ContinuationToken=resp['NextContinuationToken'])

    with tqdm(total=total_size, unit='B', unit_scale=True) as progress:
        callback = ProgressCallback(progress)

        futures = []
        for key, dest_file, size in tuples_list:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            future = s3_manager.download(
                bucket, key, str(dest_file), subscribers=[SizeCallback(size), callback]
            )
            futures.append(future)

        for future in futures:
            future.result()


def download_file(src_path, dest_path, version=None):
    bucket, key = split_path(src_path)

    if src_path.endswith('/'):
        if version is not None:
            raise HeliumException("Cannot specify a Version ID for a directory.")
        _download_dir(bucket, key, dest_path)
    else:
        _download_single_file(bucket, key, dest_path, version=version)


def download_bytes(path, version=None):
    bucket, key = split_path(path)
    params = dict(Bucket=bucket,
                  Key=key)
    if version is not None:
        params.update(dict(VersionId=version))
        
    resp = s3_client.get_object(**params)
    meta = _parse_metadata(resp)
    body = resp['Body'].read()
    return body, meta


def upload_file(src_path, dest_path, meta):
    src_file = pathlib.Path(src_path)
    is_dir = src_file.is_dir()
    if src_path.endswith('/'):
        if not is_dir:
            raise ValueError("Source path not a directory")
        if not dest_path.endswith('/'):
            raise ValueError("Destination path must end in /")
    else:
        if is_dir:
            raise ValueError("Source path is a directory; must end in /")

    bucket, key = split_path(dest_path)
    extra_args = dict(Metadata={HELIUM_METADATA: json.dumps(meta)})

    if is_dir:
        src_root = src_file
        src_file_list = list(f for f in src_file.rglob('*') if f.is_file())
    else:
        src_root = src_file.parent
        src_file_list = [src_file]

    total_size = sum(f.stat().st_size for f in src_file_list)

    with tqdm(total=total_size, unit='B', unit_scale=True) as progress:
        callback = ProgressCallback(progress)

        futures = []
        for f in src_file_list:
            real_dest_path = key + str(f.relative_to(src_root)) if (not key or key.endswith('/')) else key
            future = s3_manager.upload(str(f), bucket, real_dest_path, extra_args, [callback])
            futures.append(future)

        for future in futures:
            future.result()


def upload_bytes(data, path, meta):
    bucket, key = split_path(path)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        Metadata={HELIUM_METADATA: json.dumps(meta)}
    )


def delete_object(path):
    bucket, key = split_path(path)
    resp = s3_client.delete_object(
        Bucket=bucket,
        Key=key
    )


def list_object_versions(path, recursive=True):
    bucket, key = split_path(path)
    list_obj_params = dict(Bucket=bucket,
                           Prefix=key
                           )
    if not recursive:
        # Treat '/' as a directory separator and only return one level of files instead of everything.
        list_obj_params.update(dict(Delimiter='/'))

    # TODO: make this a generator?
    versions = []
    delete_markers = []
    prefixes = []
    more = True
    while more:
        response = s3_client.list_object_versions(**list_obj_params)
        more = response['IsTruncated']
        if more:
            next_key = response['NextKeyMarker']
            next_vid = response['NextVersionIdMarker']
            list_obj_params.update({'VersionIdMarker': next_vid,
                                    'KeyMarker': next_key})
            
        versions += response.get('Versions', [])
        delete_markers += response.get('DeleteMarkers', [])
        prefixes += response.get('CommonPrefixes', [])

    if recursive:
        return versions, delete_markers
    else:
        return prefixes, versions, delete_markers


def list_objects(path, recursive=True):
    bucket, prefix = split_path(path)
    objects = []
    prefixes = []
    list_obj_params = dict(Bucket=bucket,
                           Prefix=prefix)
    if not recursive:
        # Treat '/' as a directory separator and only return one level of files instead of everything.
        list_obj_params.update(dict(Delimiter='/'))

    #TODO: make this a generator?
    more = True
    while more:
        response = s3_client.list_objects_v2(**list_obj_params)
        more = response['IsTruncated']
        if more:
            next_token = response['NextContinuationToken']
            list_obj_params.update({'ContinuationToken': next_token})

        objects += response.get('Contents', [])
        prefixes += response.get('CommonPrefixes', [])

    if recursive:
        return objects
    else:
        return prefixes, objects
