# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.

import os
import struct
from abc import ABC, ABCMeta, abstractmethod
from enum import Enum
from typing import cast

import h5py
import numpy as np
import pyarrow.parquet as pq

from osbenchmark.exceptions import InvalidExtensionException
from osbenchmark.utils.parse import ConfigurationError


class Context(Enum):
    """DataSet context enum. Can be used to add additional context for how a
    data-set should be interpreted.
    """
    INDEX = 1
    QUERY = 2
    NEIGHBORS = 3


class DataSet(ABC):
    """DataSet interface. Used for reading data-sets from files.

    Methods:
        read: Read a chunk of data from the data-set
        seek: Get to position in the data-set
        size: Gets the number of items in the data-set
        reset: Resets internal state of data-set to beginning
    """
    __metaclass__ = ABCMeta

    BEGINNING = 0

    @abstractmethod
    def read(self, chunk_size: int):
        """Read vector for given chunk size
        @param chunk_size: limits vector size to read
        """

    @abstractmethod
    def seek(self, offset: int):
        """
        Move reader to given offset
        @param offset: value to move reader pointer to
        """

    @abstractmethod
    def size(self):
        """
        Returns size of dataset
        """

    @abstractmethod
    def reset(self):
        """
        Resets the dataset reader
        """


def get_data_set(data_set_format: str, path: str, context: Context = None, column_name: str = None):
    """
    Factory method to get instance of Dataset for given format.
    Args:
        data_set_format: File format like hdf5, bigann
        path: Data set file path
        context: Dataset Context Enum
        column_name: Dataset column name that contains expected dataset
    Returns: DataSet instance
    """
    if data_set_format == HDF5DataSet.FORMAT_NAME:
        return HDF5DataSet(path, context)
    if data_set_format == BigANNVectorDataSet.FORMAT_NAME:
        return BigANNVectorDataSet(path)
    if data_set_format == ParquetDataSet.FORMAT_NAME:
        return ParquetDataSet(path, column_name)
    raise ConfigurationError("Invalid data set format")


class HDF5DataSet(DataSet):
    """ Data-set format corresponding to `ANN Benchmarks
    <https://github.com/erikbern/ann-benchmarks#data-sets>`_
    """

    FORMAT_NAME = "hdf5"

    def __init__(self, dataset_path: str, context: Context):
        self.dataset_path = dataset_path
        self.context = self.parse_context(context)
        self.current = self.BEGINNING
        self.data = None

    def _load(self):
        if self.data is None:
            file = h5py.File(self.dataset_path)
            self.data = cast(h5py.Dataset, file[self.context])

    def read(self, chunk_size: int):
        # load file first before read
        self._load()
        if self.current >= self.size():
            return None

        end_offset = self.current + chunk_size
        if end_offset > self.size():
            end_offset = self.size()

        vectors = cast(np.ndarray, self.data[self.current:end_offset])
        self.current = end_offset
        return vectors

    def seek(self, offset: int):
        if offset < self.BEGINNING:
            raise Exception("Offset must be greater than or equal to 0")

        if offset >= self.size():
            raise Exception("Offset must be less than the data set size")

        self.current = offset

    def size(self):
        # load file first before return size
        self._load()
        return self.data.len()

    def reset(self):
        self.current = self.BEGINNING

    @staticmethod
    def parse_context(context: Context) -> str:
        if context == Context.NEIGHBORS:
            return "neighbors"

        if context == Context.INDEX:
            return "train"

        if context == Context.QUERY:
            return "test"

        raise Exception("Unsupported context")


class ParquetDataSet(DataSet):
    """ Column-oriented data storage format designed for efficient data storage and retrieval
    """

    FORMAT_NAME = "parquet"
    DEFAULT_BATCH_SIZE = 500

    def __init__(self, dataset_path: str, column: str):
        if not dataset_path:
            raise ConfigurationError("dataset path cannot be None or empty")
        if not column:
            raise ConfigurationError("dataset column name cannot be None or empty")
        self.dataset_path = dataset_path
        self.file = None
        self.column = column
        self.current = self.BEGINNING
        self.batch_iterator = None
        self.prev_batch = []
        self.file_batch_start = 0
        self.file_batch_end = 0

    def get_batch(self):
        if self.prev_batch:
            return self.prev_batch
        batch = next(self.batch_iterator)
        self.file_batch_start = self.file_batch_end
        self.file_batch_end = self.file_batch_start + len(batch)
        return batch

    def _extract_vectors(self, start_offset, end_offset):
        """
        Since Parquet files are huge, it is not advisable to load complete file in memory.
        Hence, we will iterate as a batch and extract vectors from one or multiple batches
        based on start and end offset.

        @param start_offset: start offset to extract vectors from parquet file
        @param end_offset: start offset to extract vectors from parquet file
        @return: array of vectors
        """
        vectors = []
        while True:
            batch = self.get_batch()
            if not batch:
                break
            batch_length = len(batch)
            if start_offset > self.file_batch_end:
                # run through batches till we get first batch where start_offset is inside the batch
                continue
            if start_offset < self.file_batch_start:
                # update start offset if we have to extract vectors across batches
                start_offset = self.file_batch_start
            # mark the beginning of extract
            copy_begin_offset = batch_length - (self.file_batch_end - start_offset)
            if end_offset <= self.file_batch_end:
                # mark the end of extract if end offset is within current batch
                copy_end_offset = batch_length - (self.file_batch_end - end_offset)
                vectors.extend(batch[copy_begin_offset:copy_end_offset][0].to_pandas())
                # since we haven't exhausted current batch, push this batch into memory
                # and use this as current batch for next read
                self.prev_batch = batch
                break
            # end offset is greater than current batch offset, hence, copy till end of the batch.
            vectors.extend(batch[copy_begin_offset:][0].to_pandas())
            # remove previously saved batch once complete batch is read
            self.prev_batch = []
        return np.array(vectors)

    def _load(self):
        if not self.file:
            self.file = pq.ParquetFile(self.dataset_path)
            self.batch_iterator = self.file.iter_batches(batch_size=self.DEFAULT_BATCH_SIZE, columns=[self.column])
            self.prev_batch = []
            self.file_batch_start = 0
            self.file_batch_end = 0

    def read(self, chunk_size: int):
        # load file first before read
        self._load()
        if self.current >= self.size():
            return None
        end_offset = self.current + chunk_size
        if end_offset > self.size():
            end_offset = self.size()

        vectors = self._extract_vectors(self.current, end_offset)
        self.current = end_offset
        return vectors

    def seek(self, offset: int):
        if offset < self.BEGINNING:
            raise Exception("Offset must be greater than or equal to 0")

        if offset >= self.size():
            raise Exception("Offset must be less than the data set size")

        self.current = offset

    def size(self):
        # load file first before return size
        self._load()
        return self.file.metadata.num_rows

    def reset(self):
        self.current = self.BEGINNING
        # Force all attributes to reset. This can be achieved by resetting file. Hence,
        # subsequent dataset function calls loads file again from beginning
        self.file = None


class BigANNVectorDataSet(DataSet):
    """ Data-set format for vector data-sets for `Big ANN Benchmarks
    <https://big-ann-benchmarks.com/index.html#bench-datasets>`_
    """

    DATA_SET_HEADER_LENGTH = 8
    U8BIN_EXTENSION = "u8bin"
    FBIN_EXTENSION = "fbin"
    FORMAT_NAME = "bigann"
    SUPPORTED_EXTENSION = [
        FBIN_EXTENSION, U8BIN_EXTENSION
    ]

    BYTES_PER_U8INT = 1
    BYTES_PER_FLOAT = 4

    def __init__(self, dataset_path: str):
        self.dataset_path = dataset_path
        self.file = None
        self.current = BigANNVectorDataSet.BEGINNING
        self.num_points = 0
        self.dimension = 0
        self.bytes_per_num = 0

    def _init_internal_params(self):
        self.file = open(self.dataset_path, 'rb')
        self.file.seek(BigANNVectorDataSet.BEGINNING, os.SEEK_END)
        num_bytes = self.file.tell()
        self.file.seek(BigANNVectorDataSet.BEGINNING)

        if num_bytes < BigANNVectorDataSet.DATA_SET_HEADER_LENGTH:
            raise Exception("Invalid file: file size cannot be less than {} bytes".format(
                BigANNVectorDataSet.DATA_SET_HEADER_LENGTH))

        self.num_points = int.from_bytes(self.file.read(4), "little")
        self.dimension = int.from_bytes(self.file.read(4), "little")
        self.bytes_per_num = self._get_data_size(self.dataset_path)

        if (num_bytes - BigANNVectorDataSet.DATA_SET_HEADER_LENGTH) != (
                self.num_points * self.dimension * self.bytes_per_num):
            raise Exception("Invalid file. File size is not matching with expected estimated "
                            "value based on number of points, dimension and bytes per point")

        self.reader = self._value_reader(self.dataset_path)

    def _load(self):
        # load file if it is not loaded yet
        if self.file is None:
            self._init_internal_params()

    def read(self, chunk_size: int):
        # load file first before read
        self._load()
        if self.current >= self.size():
            return None

        end_offset = self.current + chunk_size
        if end_offset > self.size():
            end_offset = self.size()

        vectors = np.asarray(
            [self._read_vector() for _ in range(end_offset - self.current)]
        )
        self.current = end_offset
        return vectors

    def seek(self, offset: int):
        # load file first before seek
        self._load()
        if offset < self.BEGINNING:
            raise Exception("Offset must be greater than or equal to 0")

        if offset >= self.size():
            raise Exception("Offset must be less than the data set size")

        bytes_offset = BigANNVectorDataSet.DATA_SET_HEADER_LENGTH + (
                self.dimension * self.bytes_per_num * offset)
        self.file.seek(bytes_offset)
        self.current = offset

    def _read_vector(self):
        return np.asarray([self.reader(self.file) for _ in
                           range(self.dimension)])

    def size(self):
        # load file first before return size
        self._load()
        return self.num_points

    def reset(self):
        if self.file:
            self.file.seek(BigANNVectorDataSet.DATA_SET_HEADER_LENGTH)
        self.current = BigANNVectorDataSet.BEGINNING

    def __del__(self):
        if self.file:
            self.file.close()

    @staticmethod
    def _get_extension(file_name):
        ext = file_name.split('.')[-1]
        if ext not in BigANNVectorDataSet.SUPPORTED_EXTENSION:
            raise InvalidExtensionException(
                "Unknown extension :{}, supported extensions are: {}".format(
                    ext, str(BigANNVectorDataSet.SUPPORTED_EXTENSION)))
        return ext

    @staticmethod
    def _get_data_size(file_name):
        ext = BigANNVectorDataSet._get_extension(file_name)
        if ext == BigANNVectorDataSet.U8BIN_EXTENSION:
            return BigANNVectorDataSet.BYTES_PER_U8INT

        if ext == BigANNVectorDataSet.FBIN_EXTENSION:
            return BigANNVectorDataSet.BYTES_PER_FLOAT

    @staticmethod
    def _value_reader(file_name):
        ext = BigANNVectorDataSet._get_extension(file_name)
        if ext == BigANNVectorDataSet.U8BIN_EXTENSION:
            return lambda file: float(int.from_bytes(file.read(BigANNVectorDataSet.BYTES_PER_U8INT), "little"))

        if ext == BigANNVectorDataSet.FBIN_EXTENSION:
            return lambda file: struct.unpack('<f', file.read(BigANNVectorDataSet.BYTES_PER_FLOAT))
