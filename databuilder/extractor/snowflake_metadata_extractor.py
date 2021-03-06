import logging
import six
from collections import namedtuple

from pyhocon import ConfigFactory, ConfigTree  # noqa: F401
from typing import Iterator, Union, Dict, Any  # noqa: F401
from unidecode import unidecode

from databuilder import Scoped
from databuilder.extractor.base_extractor import Extractor
from databuilder.extractor.sql_alchemy_extractor import SQLAlchemyExtractor
from databuilder.models.table_metadata import TableMetadata, ColumnMetadata
from itertools import groupby


TableKey = namedtuple('TableKey', ['schema_name', 'table_name'])

LOGGER = logging.getLogger(__name__)


class SnowflakeMetadataExtractor(Extractor):
    """
    Extracts Snowflake table and column metadata from underlying meta store database using SQLAlchemyExtractor.
    Requirements:
        snowflake-connector-python
        snowflake-sqlalchemy
    """
    # SELECT statement from snowflake information_schema to extract table and column metadata
    SQL_STATEMENT = """
    SELECT
        lower(c.column_name) AS col_name,
        c.comment AS col_description,
        lower(c.data_type) AS col_type,
        lower(c.ordinal_position) AS col_sort_order,
        lower(c.table_catalog) AS database,
        lower({cluster_source}) AS cluster,
        lower(c.table_schema) AS schema_name,
        lower(c.table_name) AS name,
        t.comment AS description,
        decode(lower(t.table_type), 'view', 'true', 'false') AS is_view
    FROM
        {database}.INFORMATION_SCHEMA.COLUMNS AS c
    LEFT JOIN
        {database}.INFORMATION_SCHEMA.TABLES t
            ON c.TABLE_NAME = t.TABLE_NAME
            AND c.TABLE_SCHEMA = t.TABLE_SCHEMA
    {where_clause_suffix};
    """

    # CONFIG KEYS
    WHERE_CLAUSE_SUFFIX_KEY = 'where_clause_suffix'
    CLUSTER_KEY = 'cluster_key'
    USE_CATALOG_AS_CLUSTER_NAME = 'use_catalog_as_cluster_name'
    DATABASE_KEY = 'database_key'

    # Default values
    DEFAULT_CLUSTER_NAME = 'master'

    DEFAULT_CONFIG = ConfigFactory.from_dict(
        {WHERE_CLAUSE_SUFFIX_KEY: ' ',
         CLUSTER_KEY: DEFAULT_CLUSTER_NAME,
         USE_CATALOG_AS_CLUSTER_NAME: True,
         DATABASE_KEY: 'prod'}
    )

    def init(self, conf):
        # type: (ConfigTree) -> None
        conf = conf.with_fallback(SnowflakeMetadataExtractor.DEFAULT_CONFIG)
        self._cluster = '{}'.format(conf.get_string(SnowflakeMetadataExtractor.CLUSTER_KEY))

        if conf.get_bool(SnowflakeMetadataExtractor.USE_CATALOG_AS_CLUSTER_NAME):
            cluster_source = "c.table_catalog"
        else:
            cluster_source = "'{}'".format(self._cluster)

        self._database = conf.get_string(SnowflakeMetadataExtractor.DATABASE_KEY)
        if six.PY2:
            self._database = self._database.encode('utf-8', 'ignore')

        self.sql_stmt = SnowflakeMetadataExtractor.SQL_STATEMENT.format(
            where_clause_suffix=conf.get_string(SnowflakeMetadataExtractor.WHERE_CLAUSE_SUFFIX_KEY),
            cluster_source=cluster_source,
            database=self._database
        )

        LOGGER.info('SQL for snowflake metadata: {}'.format(self.sql_stmt))

        self._alchemy_extractor = SQLAlchemyExtractor()
        sql_alch_conf = Scoped.get_scoped_conf(conf, self._alchemy_extractor.get_scope())\
            .with_fallback(ConfigFactory.from_dict({SQLAlchemyExtractor.EXTRACT_SQL: self.sql_stmt}))

        self._alchemy_extractor.init(sql_alch_conf)
        self._extract_iter = None  # type: Union[None, Iterator]

    def extract(self):
        # type: () -> Union[TableMetadata, None]
        if not self._extract_iter:
            self._extract_iter = self._get_extract_iter()
        try:
            return next(self._extract_iter)
        except StopIteration:
            return None

    def get_scope(self):
        # type: () -> str
        return 'extractor.snowflake'

    def _get_extract_iter(self):
        # type: () -> Iterator[TableMetadata]
        """
        Using itertools.groupby and raw level iterator, it groups to table and yields TableMetadata
        :return:
        """
        for key, group in groupby(self._get_raw_extract_iter(), self._get_table_key):
            columns = []

            for row in group:
                last_row = row
                columns.append(ColumnMetadata(
                               row['col_name'],
                               unidecode(row['col_description']) if row['col_description'] else None,
                               row['col_type'],
                               row['col_sort_order'])
                               )

            yield TableMetadata(self._database, last_row['cluster'],
                                last_row['schema_name'],
                                last_row['name'],
                                unidecode(last_row['description']) if last_row['description'] else None,
                                columns)

    def _get_raw_extract_iter(self):
        # type: () -> Iterator[Dict[str, Any]]
        """
        Provides iterator of result row from SQLAlchemy extractor
        :return:
        """
        row = self._alchemy_extractor.extract()
        while row:
            yield row
            row = self._alchemy_extractor.extract()

    def _get_table_key(self, row):
        # type: (Dict[str, Any]) -> Union[TableKey, None]
        """
        Table key consists of schema and table name
        :param row:
        :return:
        """
        if row:
            return TableKey(schema_name=row['schema_name'], table_name=row['name'])

        return None
