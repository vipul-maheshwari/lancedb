#  Copyright 2023 LanceDB Developers
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import annotations

import inspect
import os
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional, Union

import pyarrow as pa
from overrides import EnforceOverrides, override
from pyarrow import fs

from lancedb.common import data_to_reader, validate_schema
from lancedb.embeddings.registry import EmbeddingFunctionRegistry
from lancedb.utils.events import register_event

from .pydantic import LanceModel
from .table import AsyncLanceTable, LanceTable, Table, _sanitize_data
from .util import fs_from_uri, get_uri_location, get_uri_scheme, join_uri

if TYPE_CHECKING:
    from datetime import timedelta

    from ._lancedb import Connection as LanceDbConnection
    from .common import DATA, URI
    from .embeddings import EmbeddingFunctionConfig


class DBConnection(EnforceOverrides):
    """An active LanceDB connection interface."""

    @abstractmethod
    def table_names(
        self, page_token: Optional[str] = None, limit: int = 10
    ) -> Iterable[str]:
        """List all tables in this database, in sorted order

        Parameters
        ----------
        page_token: str, optional
            The token to use for pagination. If not present, start from the beginning.
            Typically, this token is last table name from the previous page.
            Only supported by LanceDb Cloud.
        limit: int, default 10
            The size of the page to return.
            Only supported by LanceDb Cloud.

        Returns
        -------
        Iterable of str
        """
        pass

    @abstractmethod
    def create_table(
        self,
        name: str,
        data: Optional[DATA] = None,
        schema: Optional[Union[pa.Schema, LanceModel]] = None,
        mode: str = "create",
        exist_ok: bool = False,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None,
    ) -> Table:
        """Create a [Table][lancedb.table.Table] in the database.

        Parameters
        ----------
        name: str
            The name of the table.
        data: The data to initialize the table, *optional*
            User must provide at least one of `data` or `schema`.
            Acceptable types are:

            - dict or list-of-dict

            - pandas.DataFrame

            - pyarrow.Table or pyarrow.RecordBatch
        schema: The schema of the table, *optional*
            Acceptable types are:

            - pyarrow.Schema

            - [LanceModel][lancedb.pydantic.LanceModel]
        mode: str; default "create"
            The mode to use when creating the table.
            Can be either "create" or "overwrite".
            By default, if the table already exists, an exception is raised.
            If you want to overwrite the table, use mode="overwrite".
        exist_ok: bool, default False
            If a table by the same name already exists, then raise an exception
            if exist_ok=False. If exist_ok=True, then open the existing table;
            it will not add the provided data but will validate against any
            schema that's specified.
        on_bad_vectors: str, default "error"
            What to do if any of the vectors are not the same size or contains NaNs.
            One of "error", "drop", "fill".
        fill_value: float
            The value to use when filling vectors. Only used if on_bad_vectors="fill".

        Returns
        -------
        LanceTable
            A reference to the newly created table.

        !!! note

            The vector index won't be created by default.
            To create the index, call the `create_index` method on the table.

        Examples
        --------

        Can create with list of tuples or dictionaries:

        >>> import lancedb
        >>> db = lancedb.connect("./.lancedb")
        >>> data = [{"vector": [1.1, 1.2], "lat": 45.5, "long": -122.7},
        ...         {"vector": [0.2, 1.8], "lat": 40.1, "long":  -74.1}]
        >>> db.create_table("my_table", data)
        LanceTable(connection=..., name="my_table")
        >>> db["my_table"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: double
        long: double
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]

        You can also pass a pandas DataFrame:

        >>> import pandas as pd
        >>> data = pd.DataFrame({
        ...    "vector": [[1.1, 1.2], [0.2, 1.8]],
        ...    "lat": [45.5, 40.1],
        ...    "long": [-122.7, -74.1]
        ... })
        >>> db.create_table("table2", data)
        LanceTable(connection=..., name="table2")
        >>> db["table2"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: double
        long: double
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]

        Data is converted to Arrow before being written to disk. For maximum
        control over how data is saved, either provide the PyArrow schema to
        convert to or else provide a [PyArrow Table](pyarrow.Table) directly.

        >>> custom_schema = pa.schema([
        ...   pa.field("vector", pa.list_(pa.float32(), 2)),
        ...   pa.field("lat", pa.float32()),
        ...   pa.field("long", pa.float32())
        ... ])
        >>> db.create_table("table3", data, schema = custom_schema)
        LanceTable(connection=..., name="table3")
        >>> db["table3"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: float
        long: float
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]


        It is also possible to create an table from `[Iterable[pa.RecordBatch]]`:


        >>> import pyarrow as pa
        >>> def make_batches():
        ...     for i in range(5):
        ...         yield pa.RecordBatch.from_arrays(
        ...             [
        ...                 pa.array([[3.1, 4.1], [5.9, 26.5]],
        ...                     pa.list_(pa.float32(), 2)),
        ...                 pa.array(["foo", "bar"]),
        ...                 pa.array([10.0, 20.0]),
        ...             ],
        ...             ["vector", "item", "price"],
        ...         )
        >>> schema=pa.schema([
        ...     pa.field("vector", pa.list_(pa.float32(), 2)),
        ...     pa.field("item", pa.utf8()),
        ...     pa.field("price", pa.float32()),
        ... ])
        >>> db.create_table("table4", make_batches(), schema=schema)
        LanceTable(connection=..., name="table4")

        """
        raise NotImplementedError

    def __getitem__(self, name: str) -> LanceTable:
        return self.open_table(name)

    def open_table(self, name: str) -> Table:
        """Open a Lance Table in the database.

        Parameters
        ----------
        name: str
            The name of the table.

        Returns
        -------
        A LanceTable object representing the table.
        """
        raise NotImplementedError

    def drop_table(self, name: str):
        """Drop a table from the database.

        Parameters
        ----------
        name: str
            The name of the table.
        """
        raise NotImplementedError

    def drop_database(self):
        """
        Drop database
        This is the same thing as dropping all the tables
        """
        raise NotImplementedError


class LanceDBConnection(DBConnection):
    """
    A connection to a LanceDB database.

    Parameters
    ----------
    uri: str or Path
        The root uri of the database.
    read_consistency_interval: timedelta, default None
        The interval at which to check for updates to the table from other
        processes. If None, then consistency is not checked. For performance
        reasons, this is the default. For strong consistency, set this to
        zero seconds. Then every read will check for updates from other
        processes. As a compromise, you can set this to a non-zero timedelta
        for eventual consistency. If more than that interval has passed since
        the last check, then the table will be checked for updates. Note: this
        consistency only applies to read operations. Write operations are
        always consistent.

    Examples
    --------
    >>> import lancedb
    >>> db = lancedb.connect("./.lancedb")
    >>> db.create_table("my_table", data=[{"vector": [1.1, 1.2], "b": 2},
    ...                                   {"vector": [0.5, 1.3], "b": 4}])
    LanceTable(connection=..., name="my_table")
    >>> db.create_table("another_table", data=[{"vector": [0.4, 0.4], "b": 6}])
    LanceTable(connection=..., name="another_table")
    >>> sorted(db.table_names())
    ['another_table', 'my_table']
    >>> len(db)
    2
    >>> db["my_table"]
    LanceTable(connection=..., name="my_table")
    >>> "my_table" in db
    True
    >>> db.drop_table("my_table")
    >>> db.drop_table("another_table")
    """

    def __init__(
        self, uri: URI, *, read_consistency_interval: Optional[timedelta] = None
    ):
        if not isinstance(uri, Path):
            scheme = get_uri_scheme(uri)
        is_local = isinstance(uri, Path) or scheme == "file"
        if is_local:
            if isinstance(uri, str):
                uri = Path(uri)
            uri = uri.expanduser().absolute()
            Path(uri).mkdir(parents=True, exist_ok=True)
        self._uri = str(uri)

        self._entered = False
        self.read_consistency_interval = read_consistency_interval

    def __repr__(self) -> str:
        val = f"{self.__class__.__name__}({self._uri}"
        if self.read_consistency_interval is not None:
            val += f", read_consistency_interval={repr(self.read_consistency_interval)}"
        val += ")"
        return val

    @property
    def uri(self) -> str:
        return self._uri

    @override
    def table_names(
        self, page_token: Optional[str] = None, limit: int = 10
    ) -> Iterable[str]:
        """Get the names of all tables in the database. The names are sorted.

        Returns
        -------
        Iterator of str.
            A list of table names.
        """
        try:
            filesystem = fs_from_uri(self.uri)[0]
        except pa.ArrowInvalid:
            raise NotImplementedError("Unsupported scheme: " + self.uri)

        try:
            loc = get_uri_location(self.uri)
            paths = filesystem.get_file_info(fs.FileSelector(loc))
        except FileNotFoundError:
            # It is ok if the file does not exist since it will be created
            paths = []
        tables = [
            os.path.splitext(file_info.base_name)[0]
            for file_info in paths
            if file_info.extension == "lance"
        ]
        tables.sort()
        return tables

    def __len__(self) -> int:
        return len(self.table_names())

    def __contains__(self, name: str) -> bool:
        return name in self.table_names()

    @override
    def create_table(
        self,
        name: str,
        data: Optional[DATA] = None,
        schema: Optional[Union[pa.Schema, LanceModel]] = None,
        mode: str = "create",
        exist_ok: bool = False,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None,
    ) -> LanceTable:
        """Create a table in the database.

        See
        ---
        DBConnection.create_table
        """
        if mode.lower() not in ["create", "overwrite"]:
            raise ValueError("mode must be either 'create' or 'overwrite'")

        tbl = LanceTable.create(
            self,
            name,
            data,
            schema,
            mode=mode,
            exist_ok=exist_ok,
            on_bad_vectors=on_bad_vectors,
            fill_value=fill_value,
            embedding_functions=embedding_functions,
        )
        return tbl

    @override
    def open_table(self, name: str) -> LanceTable:
        """Open a table in the database.

        Parameters
        ----------
        name: str
            The name of the table.

        Returns
        -------
        A LanceTable object representing the table.
        """
        return LanceTable.open(self, name)

    @override
    def drop_table(self, name: str, ignore_missing: bool = False):
        """Drop a table from the database.

        Parameters
        ----------
        name: str
            The name of the table.
        ignore_missing: bool, default False
            If True, ignore if the table does not exist.
        """
        try:
            filesystem, path = fs_from_uri(self.uri)
            table_path = join_uri(path, name + ".lance")
            filesystem.delete_dir(table_path)
        except FileNotFoundError:
            if not ignore_missing:
                raise

    @override
    def drop_database(self):
        filesystem, path = fs_from_uri(self.uri)
        filesystem.delete_dir(path)


class AsyncConnection(EnforceOverrides):
    """An active LanceDB connection interface."""

    @abstractmethod
    async def table_names(
        self, *, page_token: Optional[str] = None, limit: int = 10
    ) -> Iterable[str]:
        """List all tables in this database, in sorted order

        Parameters
        ----------
        page_token: str, optional
            The token to use for pagination. If not present, start from the beginning.
            Typically, this token is last table name from the previous page.
            Only supported by LanceDb Cloud.
        limit: int, default 10
            The size of the page to return.
            Only supported by LanceDb Cloud.

        Returns
        -------
        Iterable of str
        """
        pass

    @abstractmethod
    async def create_table(
        self,
        name: str,
        data: Optional[DATA] = None,
        schema: Optional[Union[pa.Schema, LanceModel]] = None,
        mode: str = "create",
        exist_ok: bool = False,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None,
    ) -> Table:
        """Create a [Table][lancedb.table.Table] in the database.

        Parameters
        ----------
        name: str
            The name of the table.
        data: The data to initialize the table, *optional*
            User must provide at least one of `data` or `schema`.
            Acceptable types are:

            - dict or list-of-dict

            - pandas.DataFrame

            - pyarrow.Table or pyarrow.RecordBatch
        schema: The schema of the table, *optional*
            Acceptable types are:

            - pyarrow.Schema

            - [LanceModel][lancedb.pydantic.LanceModel]
        mode: str; default "create"
            The mode to use when creating the table.
            Can be either "create" or "overwrite".
            By default, if the table already exists, an exception is raised.
            If you want to overwrite the table, use mode="overwrite".
        exist_ok: bool, default False
            If a table by the same name already exists, then raise an exception
            if exist_ok=False. If exist_ok=True, then open the existing table;
            it will not add the provided data but will validate against any
            schema that's specified.
        on_bad_vectors: str, default "error"
            What to do if any of the vectors are not the same size or contains NaNs.
            One of "error", "drop", "fill".
        fill_value: float
            The value to use when filling vectors. Only used if on_bad_vectors="fill".

        Returns
        -------
        LanceTable
            A reference to the newly created table.

        !!! note

            The vector index won't be created by default.
            To create the index, call the `create_index` method on the table.

        Examples
        --------

        Can create with list of tuples or dictionaries:

        >>> import lancedb
        >>> db = lancedb.connect("./.lancedb")
        >>> data = [{"vector": [1.1, 1.2], "lat": 45.5, "long": -122.7},
        ...         {"vector": [0.2, 1.8], "lat": 40.1, "long":  -74.1}]
        >>> db.create_table("my_table", data)
        LanceTable(connection=..., name="my_table")
        >>> db["my_table"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: double
        long: double
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]

        You can also pass a pandas DataFrame:

        >>> import pandas as pd
        >>> data = pd.DataFrame({
        ...    "vector": [[1.1, 1.2], [0.2, 1.8]],
        ...    "lat": [45.5, 40.1],
        ...    "long": [-122.7, -74.1]
        ... })
        >>> db.create_table("table2", data)
        LanceTable(connection=..., name="table2")
        >>> db["table2"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: double
        long: double
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]

        Data is converted to Arrow before being written to disk. For maximum
        control over how data is saved, either provide the PyArrow schema to
        convert to or else provide a [PyArrow Table](pyarrow.Table) directly.

        >>> custom_schema = pa.schema([
        ...   pa.field("vector", pa.list_(pa.float32(), 2)),
        ...   pa.field("lat", pa.float32()),
        ...   pa.field("long", pa.float32())
        ... ])
        >>> db.create_table("table3", data, schema = custom_schema)
        LanceTable(connection=..., name="table3")
        >>> db["table3"].head()
        pyarrow.Table
        vector: fixed_size_list<item: float>[2]
          child 0, item: float
        lat: float
        long: float
        ----
        vector: [[[1.1,1.2],[0.2,1.8]]]
        lat: [[45.5,40.1]]
        long: [[-122.7,-74.1]]


        It is also possible to create an table from `[Iterable[pa.RecordBatch]]`:


        >>> import pyarrow as pa
        >>> def make_batches():
        ...     for i in range(5):
        ...         yield pa.RecordBatch.from_arrays(
        ...             [
        ...                 pa.array([[3.1, 4.1], [5.9, 26.5]],
        ...                     pa.list_(pa.float32(), 2)),
        ...                 pa.array(["foo", "bar"]),
        ...                 pa.array([10.0, 20.0]),
        ...             ],
        ...             ["vector", "item", "price"],
        ...         )
        >>> schema=pa.schema([
        ...     pa.field("vector", pa.list_(pa.float32(), 2)),
        ...     pa.field("item", pa.utf8()),
        ...     pa.field("price", pa.float32()),
        ... ])
        >>> db.create_table("table4", make_batches(), schema=schema)
        LanceTable(connection=..., name="table4")

        """
        raise NotImplementedError

    async def open_table(self, name: str) -> Table:
        """Open a Lance Table in the database.

        Parameters
        ----------
        name: str
            The name of the table.

        Returns
        -------
        A LanceTable object representing the table.
        """
        raise NotImplementedError

    async def drop_table(self, name: str):
        """Drop a table from the database.

        Parameters
        ----------
        name: str
            The name of the table.
        """
        raise NotImplementedError

    async def drop_database(self):
        """
        Drop database
        This is the same thing as dropping all the tables
        """
        raise NotImplementedError


class AsyncLanceDBConnection(AsyncConnection):
    def __init__(self, connection: LanceDbConnection):
        self._inner = connection

    async def __repr__(self) -> str:
        pass

    @override
    async def table_names(
        self,
        *,
        page_token=None,
        limit=None,
    ) -> Iterable[str]:
        # TODO: hook in page_token and limit
        return await self._inner.table_names()

    @override
    async def create_table(
        self,
        name: str,
        data: Optional[DATA] = None,
        schema: Optional[Union[pa.Schema, LanceModel]] = None,
        mode: str = "create",
        exist_ok: bool = False,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None,
    ) -> Table:
        if mode.lower() not in ["create", "overwrite"]:
            raise ValueError("mode must be either 'create' or 'overwrite'")

        if inspect.isclass(schema) and issubclass(schema, LanceModel):
            # convert LanceModel to pyarrow schema
            # note that it's possible this contains
            # embedding function metadata already
            schema = schema.to_arrow_schema()

        metadata = None
        if embedding_functions is not None:
            # If we passed in embedding functions explicitly
            # then we'll override any schema metadata that
            # may was implicitly specified by the LanceModel schema
            registry = EmbeddingFunctionRegistry.get_instance()
            metadata = registry.get_table_metadata(embedding_functions)

        if data is not None:
            data = _sanitize_data(
                data,
                schema,
                metadata=metadata,
                on_bad_vectors=on_bad_vectors,
                fill_value=fill_value,
            )

        if schema is None:
            if data is None:
                raise ValueError("Either data or schema must be provided")
            elif hasattr(data, "schema"):
                schema = data.schema
            elif isinstance(data, Iterable):
                if metadata:
                    raise TypeError(
                        (
                            "Persistent embedding functions not yet "
                            "supported for generator data input"
                        )
                    )

        if metadata:
            schema = schema.with_metadata(metadata)
        validate_schema(schema)

        if mode == "create" and exist_ok:
            mode = "exist_ok"

        if data is None:
            new_table = await self._inner.create_empty_table(name, mode, schema)
        else:
            data = data_to_reader(data, schema)
            new_table = await self._inner.create_table(
                name,
                mode,
                data,
            )

        register_event("create_table")
        return AsyncLanceTable(new_table)

    @override
    async def open_table(self, name: str) -> LanceTable:
        raise NotImplementedError

    @override
    async def drop_table(self, name: str, ignore_missing: bool = False):
        raise NotImplementedError

    @override
    async def drop_database(self):
        raise NotImplementedError
