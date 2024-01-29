# Copyright 2022-2024 MosaicML Streaming authors
# SPDX-License-Identifier: Apache-2.0

"""Serialize samples into streaming dataset shards and index."""

from abc import abstractmethod
from typing import Any, List, Optional, Tuple, Union

from streaming.format.base.writer.row import RowWriter

__all__ = ['DualRowWriter']


class DualRowWriter(RowWriter):
    """Writes a streaming dataset with dual shards.

    Dual shards refer to raw data (csv, json, etc.) paired with an index into it.

    Args:
        out (str | Tuple[str, str]): Output dataset directory to save shard files.

            1. If ``out`` is a local directory, shard files are saved locally.
            2. If ``out`` is a remote directory, a local temporary directory is created to
               cache the shard files and then the shard files are uploaded to a remote
               location. At the end, the temp directory is deleted once shards are uploaded.
            3. If ``out`` is a tuple of ``(local_dir, remote_dir)``, shard files are saved in the
               `local_dir` and also uploaded to a remote location.
        keep_local (bool): If the dataset is uploaded, whether to keep the local dataset directory
            or remove it after uploading. Defaults to ``False``.
        compression (str, optional): Optional compression or compression:level. Defaults to
            ``None``.
        hashes (List[str], optional): Optional list of hash algorithms to apply to shard files.
            Defaults to ``None``.
        size_limit (int, optional): Optional shard size limit, after which point to start a new
            shard. If None, puts everything in one shard. Defaults to ``1 << 26``.
        **kwargs (Any): Additional settings for the RowWriter.

            progress_bar (bool): Display TQDM progress bars for uploading output dataset files to
                a remote location. Default to ``False``.
            max_workers (int): Maximum number of threads used to upload output dataset files in
                parallel to a remote location. One thread is responsible for uploading one shard
                file to a remote location. Default to ``min(32, (os.cpu_count() or 1) + 4)``.
            retry (int): Number of times to retry uploading a file to a remote location.
                Default to ``2``.
    """

    extra_bytes_per_shard = 0
    extra_bytes_per_sample = 0

    def __init__(self,
                 *,
                 out: Union[str, Tuple[str, str]],
                 keep_local: bool = False,
                 compression: Optional[str] = None,
                 hashes: Optional[List[str]] = None,
                 size_limit: Optional[Union[int, str]] = 1 << 26,
                 **kwargs: Any) -> None:
        super().__init__(out=out,
                         keep_local=keep_local,
                         compression=compression,
                         hashes=hashes,
                         size_limit=size_limit,
                         extra_bytes_per_shard=self.extra_bytes_per_shard,
                         extra_bytes_per_sample=self.extra_bytes_per_sample,
                         **kwargs)

    @abstractmethod
    def encode_dual_shard(self) -> Tuple[bytes, bytes]:
        """Encode a dual shard out of the cached samples (data, meta files).

        Returns:
            Tuple[bytes, bytes]: Data file, meta file.
        """
        raise NotImplementedError

    def flush_shard(self) -> None:
        if self.event.is_set():
            # Shutdown the executor and cancel all the pending futures due to exception in one of
            # the threads.
            self.cancel_future_jobs()
            return

        raw_data_basename, zip_data_basename = self._name_next_shard()
        raw_meta_basename, zip_meta_basename = self._name_next_shard('meta')
        raw_data, raw_meta = self.encode_dual_shard()
        raw_data_info, zip_data_info = self._process_file(raw_data, raw_data_basename,
                                                          zip_data_basename)
        raw_meta_info, zip_meta_info = self._process_file(raw_meta, raw_meta_basename,
                                                          zip_meta_basename)
        obj = {
            'samples': len(self.new_samples),
            'raw_data': raw_data_info,
            'zip_data': zip_data_info,
            'raw_meta': raw_meta_info,
            'zip_meta': zip_meta_info
        }
        obj.update(self.get_config())
        self.shards.append(obj)

        # Execute the task if there is no exception in any of the async threads.
        future = self.executor.submit(self.cloud_writer.upload_file, zip_data_basename or
                                      raw_data_basename)
        future.add_done_callback(self.exception_callback)

        # Execute the task if there is no exception in any of the async threads.
        future = self.executor.submit(self.cloud_writer.upload_file, zip_meta_basename or
                                      raw_meta_basename)
        future.add_done_callback(self.exception_callback)