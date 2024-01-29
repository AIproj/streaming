# Copyright 2022-2024 MosaicML Streaming authors
# SPDX-License-Identifier: Apache-2.0

"""An individual file of a Streaming shard."""

import os
from typing import Optional, Set

import numpy as np
from numpy.typing import NDArray

from streaming.compression import decompress
from streaming.format.base.canonical import canonicalize
from streaming.format.base.phase import ShardFilePhase
from streaming.format.base.phaser import Locality
from streaming.format.base.stream_conf import StreamConf

__all__ = ['ShardFile']


class ShardFile:
    """Metadata for validating every phase of file across decompression and/or preparation.

            *
            |
        Downloading
            |
          [zip]
            |
        Decompression
            |
          [raw]
            |
        Canonicalization
            |
          [can]
            |
        Eviction
            |
            v

    Shard formats:
      * MDS: zip -> raw (raw phase is performant, so no need for a can phase).
      * JSONL: zip -> raw (").
      * XSV: zip -> raw (").
      * Parquet: raw -> can (using pre-existing third-party Parquet files, so can't compress, and
        Parquet from-disk random access is bad enough to require converting to a usable format).

    Notes:
      * All shard files are required to have a raw phase.
      * Every non-index file of a serialized Streaming dataset belongs to some Shard.
      * Currently, all types of Shard are comprised of either one or two shard files.
      * All files of a Shard must either jointly exist or not exist in order to be in a coherent
        state.
      * Files transition across their applicable phases unidirectionally forward.
      * Files are stored officially, and therefore downloaded, in their first phase, and used in
        their last phase.

    Args:
        stream (StreamConf): Link back up to the Stream that owns this shard, from which we get
            arguments which are shared across all shards like remote/local paths. Avoids an import
            cycle by Stream subclassing StreamConf.
        zip_phase (ShardFilePhase, optional): Metadata for validating the compressed phase of the
            file. Defaults to ``None``.
        zip_algo (str, optional): Decompression algorithm, if zip is used. Defaults to ``None``.
        raw_phase (ShardFilePhase): Metadata for validating the regular phase of the file.
        can_algo (str, optional): Canonicalization algorithm, if can is used. Defaults to ``None``.
        can_phase (ShardFilePhase, optional): Metadata for validating the canonicalized phase of
            the file. Defaults to ``None``.
    """

    def __init__(self,
                 *,
                 stream: StreamConf,
                 zip_phase: Optional[ShardFilePhase] = None,
                 zip_algo: Optional[str] = None,
                 raw_phase: ShardFilePhase,
                 can_algo: Optional[str] = None,
                 can_phase: Optional[ShardFilePhase] = None) -> None:
        self.stream = stream
        self.zip_phase = zip_phase
        self.zip_algo = zip_algo
        self.raw_phase = raw_phase
        self.can_algo = can_algo
        self.can_phase = can_phase
        self.phases = zip_phase, raw_phase, can_phase

    def set_stream(self, stream: StreamConf) -> None:
        """Save a link to the owning Stream, as many Stream args apply to all its Shards.

        Args:
            stream (StreamConf): The Stream that owns this Shard.
        """
        self.stream = stream
        for phase in self.phases:
            if phase:
                phase.set_stream(stream)

    def validate(self) -> None:
        """Check whether this file is acceptable to be part of some Stream."""
        # If we have a size limit on downloads, verify that it is not exceeded by our first phase.
        if self.stream.download_max_size is not None:
            for phase in self.phases:
                if phase:
                    phase.validate_for_download()
                break

    def locate(self, listing: Set[str]) -> NDArray[np.int64]:
        """Probe the given directory listing for each phase of this file.

        Try to minimize hitting the filesystem for performance resaons.

        Args:
            listing (Set[str]): Recursive dataset file listing.

        Returns:
            NDArray[np.int64]: Whether each phase of this file is present or not in the listing.
        """
        arr = np.ndarray(len(self.phases), np.int64)
        for phase_id, phase in enumerate(self.phases):
            if phase:
                arr[phase_id] = Locality.LOCAL if phase.probe(listing) else Locality.REMOTE
            else:
                arr[phase_id] = Locality.DNE
        return arr

    def init_dir(self, listing: Set[str]) -> int:
        """Normalize this file's phases' presence in the local directory to a coherent state.

        Args:
            listing (Set[str]): Recursive dataset file listing.

        Returns:
            int: Disk usage.
        """
        # Collect this file's current cache usage, and the locality of each of its phases.
        phase_locs = []
        du = 0
        for phase in self.phases:
            if phase:
                phase_du = phase.init_dir(listing)
                if phase_du:
                    du += phase_du
                    phase_loc = Locality.LOCAL
                else:
                    phase_loc = Locality.REMOTE
            else:
                phase_loc = Locality.DNE
            phase_locs.append(phase_loc)
        phase_locs = np.array(phase_locs, np.int64)

        # From phase localities, determine phase evictions according to the keep policy.
        phase_dels = self.stream.safe_keep_phases.get_phase_deletions(phase_locs)

        # Apply any evictions.
        du += self.evict_phases(phase_dels)

        # Finally, return our local dir usage after evictions.
        return du

    def _unzip(self) -> int:
        """Decompress zip phase, resulting in raw phase, maybe deleting zip phase.

        Returns:
            int: Delta disk usage, in bytes.
        """
        if not self.zip_phase:
            raise RuntimeError('Wanted to unzip a shard, but required metadata is missing.')

        # Read the zip phase data.
        zip_filename = self.zip_phase.get_local_filename()
        zip_data = open(zip_filename, 'rb').read()

        # Mandatory cheap compressed phase size check.
        if len(zip_data) != self.zip_phase.size:
            raise ValueError(f'Compressed data does not match the expected size: ' +
                             f'{len(zip_data):,} bytes vs {self.zip_phase.size:,} bytes.')

        # Decompress.
        raw_data = decompress(self.zip_algo, zip_data)

        # Mandatory cheap decompressed phase size check.
        if len(raw_data) != self.raw_phase.size:
            raise ValueError(f'Decompressed data does not match the expected size: ' +
                             f'{len(raw_data):,} bytes vs {self.raw_phase.size:,} bytes.')

        # Save the raw phase data.
        raw_filename = self.raw_phase.get_local_filename()
        tmp_filename = raw_filename + '.tmp'
        with open(tmp_filename, 'wb') as out:
            out.write(raw_data)
        os.rename(tmp_filename, raw_filename)

        # Collect the locality of each phase.
        if self.can_phase:
            can_loc = Locality.LOCAL if self.can_phase.is_local() else Locality.REMOTE
        else:
            can_loc = Locality.DNE
        phase_locs = np.array([Locality.LOCAL, Locality.LOCAL, can_loc], np.int64)

        # Given localities and policy, determine phase evictions.
        phase_dels = self.stream.safe_keep_phases.get_phase_deletions(phase_locs)

        # Delete phases we don't want.
        return len(raw_data) + self.evict_phases(phase_dels)

    def _canonicalize(self) -> int:
        """Canonicalize raw phase, resulting in can phase, maybe deleting raw phase.

        Returns:
            int: Delta disk usage, in bytes.
        """
        if not self.can_algo or not self.can_phase:
            raise RuntimeError('Wanted to canonicalize a shard, but required metadata is missing.')

        # Canonicalize the file from raw to can phases.
        raw_filename = self.raw_phase.get_local_filename()
        can_filename = self.can_phase.get_local_filename()
        canonicalize(self.can_algo, raw_filename, can_filename)

        # Mandatory cheap canonicalized phase size check.
        can_size = os.stat(can_filename).st_size
        if self.can_phase.size is not None:
            if can_size != self.can_phase.size:
                raise ValueError(
                    f'Canonicalized data does not match the expected size: {can_size} vs ' +
                    f'{self.can_phase.size}.')

        # Collect the locality of each phase.
        if self.zip_phase:
            zip_loc = Locality.LOCAL if self.zip_phase.is_local() else Locality.REMOTE
        else:
            zip_loc = Locality.DNE
        phase_locs = np.array([zip_loc, Locality.LOCAL, Locality.LOCAL], np.int64)

        # Given localities and policy, determine phase evictions.
        phase_dels = self.stream.safe_keep_phases.get_phase_deletions(phase_locs)

        # Delete phases we don't want.
        return can_size + self.evict_phases(phase_dels)

    def _load_raw(self) -> int:
        """Sub-method to prepare up to the regular phase of this file.

        Returns:
            int: Delta disk usage, in bytes.
        """
        if self.raw_phase.is_local():
            ddu = 0
        else:
            if self.zip_phase:
                if self.zip_phase.is_local():
                    ddu = self._unzip()
                else:
                    ddu = self.zip_phase.download() + self._unzip()
            else:
                ddu = self.raw_phase.download()
        return ddu

    def load(self) -> int:
        """Download and/or unzip and/or canonicalize this file to being ready for use.

        Returns:
            int: Delta disk usage, in bytes.
        """
        if self.can_phase:
            if self.can_phase.is_local():
                ddu = 0
            else:
                ddu = self._load_raw() + self._canonicalize()
        else:
            ddu = self._load_raw()
        return ddu

    def evict(self) -> int:
        """Delete all phases of this file.

        Returns:
            int: Delta disk usage, in bytes.
        """
        ddu = 0
        for phase in self.phases:
            if phase:
                ddu += phase.evict()
        return ddu

    def evict_phases(self, phase_dels: NDArray[np.int64]) -> int:
        """Delete specified phases of this file.

        Returns:
            int: Delta disk usage, in bytes.
        """
        ddu = 0
        for phase, phase_del in zip(self.phases, phase_dels):
            if phase_del:
                if not phase:
                    raise RuntimeError('Internal error: attempted to evict a phase that is not ' +
                                       'valid.')
                ddu += phase.evict()
        return ddu