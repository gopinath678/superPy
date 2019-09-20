import os
import sys
import json
import base64
import logging

import pymysql.cursors
import pandas as pd


LOG_DIR = os.getenv("SUPERQUERY_LOGDIR")
LOG_LEVEL = os.getenv("SUPERQUERY_LOGLEVEL") or logging.INFO

def setup_logging():
    loglevel = int(LOG_LEVEL)
    fmt = "%(asctime)s - [%(name)s] ...%(message)s"
    logger = logging.getLogger("sQ")
    formatter = logging.Formatter(fmt)
    logger.setLevel(loglevel)
    if LOG_DIR is not None:
        logfname = os.path.join(LOG_DIR, logname + ".log")
        ofstream = logging.handlers.TimedRotatingFileHandler(logfname, when="D", interval=1, encoding="utf-8")
        ofstream.setFormatter(formatter)
        logger.addHandler(ofstream)
    #Console output:
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger

LOGGER = setup_logging()


class QueryJobConfig(object):

    def __init__(self):
        self.destination = None

class Row(object):
    def __init__(self, rowdict):
        self.__dict__ = rowdict

    def __getitem__(self, key):
        return self.__dict__[key]

    def __str__(self):
        return str(self.to_dict())

    def __repr__(self):
        return self.__str__()

    def to_dict(self):
        return self.__dict__


class Result:
    def __init__(self, cur=None, stats=None):
        self._cur = cur
        self.stats = Row(stats)
        self.destinationTable = None

    def __iter__(self):
        return self.result()

    def _set_job_reference(self, jobRef):
        for key, value in jobRef.items():
            setattr(self, key, jobRef[key])

    def result(self):
        while True:
            row = self._cur.fetchone()
            if row is not None:
                row = Row(row)
                yield row
            else:
                break

    def to_df(self):
        return pd.DataFrame([row.to_dict() for row in self])


    def set_destination_table(self, table_reference):
        if self.destinationTable:
            projectId, datasetId, tableId = table_reference.split('.')
            self.destinationTable['projectId'] = projectId
            self.destinationTable['datasetId'] = datasetId
            self.destinationTable['tableId'] = tableId

    def get_destination_table(self):
        if self.destinationTable:
            full_destination_table = '.'.join([self.destinationTable['projectId'],
                                               self.destinationTable['datasetId'],
                                               self.destinationTable['tableId']])
            return full_destination_table
        else:
            return None

    def get_key(self):
        full_destination_table = self.get_destination_table()

        if full_destination_table:
            return Result._encode_table_reference_key(full_destination_table)
        else:
            raise ValueError('Destination table metadata required to create key')

    @staticmethod
    def _encode_table_reference_key(full_destination_table):
        full_destination_table_bytes_like = full_destination_table.encode()
        b64_key = base64.b64encode(full_destination_table_bytes_like)
        return b64_key.decode()

    @staticmethod
    def decode_table_reference_key(key):
        bytes_like_key = key.encode()
        bytes_like_table_reference = base64.b64decode(bytes_like_key)
        return bytes_like_table_reference.decode()


class Client(object):

    def __init__(self):
        self._logger = logging.getLogger("sQ")

        self._username = os.getenv("SUPERQUERY_USERNAME")
        self._password = os.getenv("SUPERQUERY_PASSWORD")
        self._user_agent = "python"
        self._project = None
        self._destination_dataset = None
        self._destination_project = None
        self._destination_table = None
        self._write_disposition = None

        self.last_result = None
        self.connection = None


    def project(self, project):
        self._project = project
        return self

    def dataset(self, dataset):
        self._destination_dataset = dataset
        return self

    def table(self, table):
        self._destination_table = table
        return self

    def write_disposition(self, disposition):
        self._write_disposition = disposition
        return self

    def destination_project(self, project):
        self._destination_project = project
        return self

    def get_data_by_key(self, key, **kwargs):
        decoded_table_reference = Result.decode_table_reference_key(key)
        sql = 'SELECT * FROM `{}`'.format(decoded_table_reference)
        keyed_result = self.query(sql=sql, **kwargs)
        keyed_result.set_destination_table(decoded_table_reference)
        return keyed_result

    def query(self,
              sql,
              project=None,
              dry_run=False,
              use_cache=True,
              username=None,
              password=None,
              close_connection_afterwards=True,
              job_config=None):
        
        if job_config is not None:
            raise NotImplementedError("The job_config parameter is not yet handled")

        username = username or self._username
        password = password or self._password
        if username is None or password is None:
            raise Exception("Username or password not specified")

        try:
            #Reuse or establish connection:
            if self.connection is None:
                self._logger.debug("Establishing a new connection")
                self.connection = self.authenticate_connection(username, password)
                if self.connection is None:
                    raise Exception("Unable to establish a connection")
                else:
                    self._logger.debug("Connection to superQuery successful")
            else:
                self._logger.debug("Using existing superQuery connection!")

            #We have a connection:
            self._set_destination_project()
            self._set_destination_dataset()
            self._set_destination_table()
            self._set_write_disposition()
            self._set_dry_run(dry_run)
            self._set_caching(use_cache)
            self._set_user_agent()
            self._set_project(project)

            #We have a successful setup: 
            with self.connection.cursor() as cursor:
                cursor.execute(sql)
                with self.connection.cursor() as cursor2:
                    cursor2.execute("explain;")
                    explain = cursor2.fetchall()
                    stats = json.loads(explain[0]["statistics"])
                    #job_reference = json.loads(explain[0]["jobReference"])
                result = Result(cursor, stats)
                self.last_result = result
                return result

        except Exception as e:
            self._logger.error("An error occurred (perhaps retry)")
            self._logger.exception(e)
            self.connection = None

        finally:
            if close_connection_afterwards:
                self.close_connection()

    def close_connection(self):
        if (self.connection):
            self._logger.debug("Closing the connection")
            self.connection.close()
            self.connection = None

    def _set_user_agent(self):
        self.connection._execute_command(3, "SET super_userAgent=" + self._user_agent)
        self.connection._read_ok_packet()

    def _set_project(self, project=None):
        project = project or self._project
        if project is not None:
            self._logger.debug("Setting the project to %s", project)
            self.connection._execute_command(3, "SET super_projectId=" + project)
            self.connection._read_ok_packet()

    def _set_destination_project(self):
        project = self._destination_project or self._project
        if project is not None:
            self._logger.debug("Setting the destination project to %s", project)
            self.connection._execute_command(3, "SET super_destinationProject=" + project)
            self.connection._read_ok_packet()

    def _set_destination_dataset(self):
        if self._destination_dataset is not None:
            self._logger.debug("Setting the destination dataset to %s", self._destination_dataset)
            self.connection._execute_command(3, "SET super_destinationDataset=" + self._destination_dataset)
            self.connection._read_ok_packet()

    def _set_destination_table(self):
        if self._destination_table is not None:
            self._logger.debug("Setting the destination table to %s", self._destination_table)
            self.connection._execute_command(3, "SET super_destinationTable=" + self._destination_table)
            self.connection._read_ok_packet()

    def _set_write_disposition(self):
        disposition = self._write_disposition or "WRITE_EMPTY"
        self._logger.debug("Setting write-disposition to %s", disposition)
        self.connection._execute_command(3, "SET super_destinationWriteDisposition=" + disposition)
        self.connection._read_ok_packet()

    def _set_dry_run(self, is_dryrun=False):
        self._logger.debug("Setting dry run to %s", str(is_dryrun))
        if is_dryrun:
            self.connection._execute_command(3, "SET super_isDryRun=true")
        else:
            self.connection._execute_command(3, "SET super_isDryRun=false")
        self.connection._read_ok_packet()

    def _set_caching(self, use_cache=True):
        self._logger.debug("Setting cache to %s", str(use_cache))
        if use_cache:
            self.connection._execute_command(3, "SET super_useCache=true")
        else:
            self.connection._execute_command(3, "SET super_useCache=false")
        self.connection._read_ok_packet()

    def authenticate_connection(self, username, password, hostname="bi.superquery.io", port=3306):
        try:
            connection = pymysql.connect(
                host=hostname,
                port=port,
                user=username,
                password=password,
                db="",
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor)
            return connection
        except Exception as e:
            self._logger.debug("Authentication problem!")
            self._logger.exception(e)
            raise

