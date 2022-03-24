import os
import sqlite3
import requests

from ast import literal_eval
from typing import Dict, List, Union, Optional
from datetime import datetime

from mindsdb.integrations.libs.base_handler import PredictiveHandler
from mindsdb.integrations.libs.storage_handler import SqliteStorageHandler
from mindsdb.utilities.config import Config
from mindsdb import __version__ as mindsdb_version
from mindsdb.utilities.functions import mark_process
from lightwood.api.types import ProblemDefinition
import mindsdb.interfaces.storage.db as db
from mindsdb.interfaces.model.model_controller import ModelController
from mindsdb_sql import parse_sql
from mindsdb_sql.parser.ast import Join
from mindsdb_sql.parser.dialects.mindsdb import (
    CreateDatasource,
    RetrainPredictor,
    CreatePredictor,
    DropDatasource,
    DropPredictor,
    CreateView
)

import mlflow
from mlflow.tracking import MlflowClient
import pandas as pd


class MLflowIntegration(PredictiveHandler):
    def __init__(self, name):
        """
        An MLflow integration needs to have a working connection to work. For this:
            - All models to use should be previously served
            - An mlflow server should be running, to access the model registry
            
        Example:
            1. Run `mlflow server -p 5001 --backend-store-uri sqlite:///mlflow.db --default-artifact-root ./artifacts --host 0.0.0.0`
            2. Run `mlflow models serve --model-uri ./model_path`
            3. Instance this integration and call the `connect method` passing the relevant urls to mlflow and to the DB
            
        Note: above, `artifacts` is a folder to store artifacts for new experiments that do not specify an artifact store.
        """  # noqa
        super().__init__(name)
        self.mlflow_server_url = None
        self.mlflow_server_path = None
        self.connection = None
        self.parser = parse_sql
        self.dialect = 'mindsdb'
        mdb_config = Config()
        db_path = mdb_config['paths']['root']
        self.storage = SqliteStorageHandler(context=name, config={'path': db_path})

    def connect(self, **kwargs) -> Dict[str, int]:
        """ Connect to the mlflow process using MlflowClient class. """  # noqa
        self.mlflow_server_url = kwargs['mlflow_server_url']
        self.mlflow_server_path = kwargs['model_registry_path']
        self.connection = MlflowClient(self.mlflow_server_url, self.mlflow_server_path)
        return self.check_status()

    def check_status(self) -> Dict[str, int]:
        """ Checks that the connection is, as expected, an MlflowClient instance. """  # noqa
        # todo: as it stands this does not truly check if the connection is alive...
        try:
            assert isinstance(self.connection, mlflow.tracking.MlflowClient)
        except AssertionError as e:
            return {'status': '503', 'error': e}
        return {'status': '200'}

    def get_tables(self) -> List:
        """ Returns list of model names (that have been succesfully linked with CREATE PREDICTOR) """  # noqa
        return [row[0] for row in self.storage.get('model_list')]

    def describe_table(self, table_name: str) -> Dict:
        """ For getting standard info about a table. e.g. data types """  # noqa
        if table_name not in self.get_tables():
            raise Exception("Table not found.")

        models = {model.name: model for model in self.connection.list_registered_models()}
        model = models[table_name]
        latest_version = model.latest_versions[-1]
        description = {
            'NAME': model.name,
            'USER_DESCRIPTION': model.description,
            'LAST_STATUS': latest_version.status,
            'CREATED_AT': datetime.fromtimestamp(model.creation_timestamp//1000).strftime("%m/%d/%Y, %H:%M:%S"),
            'LAST_UPDATED': datetime.fromtimestamp(model.last_updated_timestamp//1000).strftime("%m/%d/%Y, %H:%M:%S"),
            'TAGS': model.tags,
            'LAST_RUN_ID': latest_version.run_id,
            'LAST_SOURCE_PATH': latest_version.source,
            'LAST_USER_ID': latest_version.user_id,
            'LAST_VERSION': latest_version.version,
        }
        return description

    def run_native_query(self, query_str: str) -> Optional[object]:
        statement = self.parser(query_str, dialect=self.dialect)

        if type(statement) == CreatePredictor:
            model_name = statement.name.parts[-1]

            # check that it exists within mlflow and is not already registered
            mlflow_models = [model.name for model in self.connection.list_registered_models()]
            if model_name not in mlflow_models:
                print("Error: this predictor is not registered in mlflow. Check you are serving it and try again.")
            elif model_name in self.get_tables():
                # @TODO: maybe add re-wiring so that a predictor name can point to a new endpoint?
                # e.g. add _edit_invocation_url method that edits the db.record and self.internal_registry
                print("Error: this model is already registered!")
            else:
                target = statement.targets[0].parts[-1]  # TODO: multiple target support?
                params = {
                    'format': statement.using['format'],
                    'dtype_dict': statement.using['dtype_dict'],
                    'target': target,
                    'url': statement.using['url.predict']
                }
                all_models = self.storage.get('models', {})
                all_models['model_name'] = {'params': params}
                self.storage.set('models', all_models)

        elif type(statement) == DropPredictor:
            to_drop = statement.name.parts[-1]
            all_models = self.storage.get('models')
            del all_models[to_drop]
            self.storage.set('models', all_models)

        else:
            raise Exception(f"Query type {type(statement)} not supported")

    def select_query(self, stmt) -> pd.DataFrame:
        _, _, target, model_url = self._get_model(stmt)

        if target not in [str(t) for t in stmt.targets]:
            raise Exception("Predictor will not be called, target column is not specified.")

        df = pd.DataFrame.from_dict({stmt.where.args[0].parts[0]: [stmt.where.args[1].value]})
        return self._call_model(df, model_url)


    def join(self, stmt, data_handler: BaseHandler) -> pd.DataFrame:
        """
        Batch prediction using the output of a query passed to a data handler as input for the model.
        """  # noqa

        # tag data and predictive handlers
        if len(stmt.from_table.left.parts) == 1:
            model_clause = 'left'
            data_clause = 'right'
        else:
            model_clause = 'right'
            data_clause = 'left'
        model_alias = str(getattr(stmt.from_table, model_clause).alias)

        # get model input
        data_handler_table = getattr(stmt.from_table, data_clause).parts[-1]  # todo should be ".".join(...) if data handlers support more than one table
        data_handler_cols = list(set([t.parts[-1] for t in stmt.targets]))

        data_query = f"""SELECT {','.join(data_handler_cols)} FROM {data_handler_table}"""
        if stmt.where:
            data_query += f" WHERE {str(stmt.where)}"
        if stmt.limit:
            # todo integration should handle this depending on type of query... e.g. if it is TS, then we have to fetch correct groups first and limit later
            data_query += f" LIMIT {stmt.limit.value}"

        parsed_query = self.parser(data_query, dialect=self.dialect)
        model_input = pd.DataFrame.from_records(data_handler.select_query(parsed_query))

        # rename columns
        aliased_columns = list(model_input.columns)
        for col in stmt.targets:
            if str(col.parts[0]) != model_alias and col.alias is not None:
                # assumes mdb_sql will alert if there are two columns with the same alias
                aliased_columns[aliased_columns.index(col.parts[-1])] = str(col.alias)
        model_input.columns = aliased_columns

        # get model output
        _, _, _, model_url = self._get_model(stmt)
        predictions = self._call_model(model_input, model_url)

        # rename columns
        aliased_columns = list(predictions.columns)
        for col in stmt.targets:
            if col.parts[0] == model_alias and col.alias is not None:
                aliased_columns[aliased_columns.index('prediction')] = str(col.alias)
        predictions.columns = aliased_columns

        return predictions

    def _get_model(self, stmt):
        if type(stmt.from_table) == Join:
            model_name = stmt.from_table.right.parts[-1]
        else:
            model_name = stmt.from_table.parts[-1]

        mlflow_models = [model.name for model in self.connection.list_registered_models()]
        if not model_name in self.get_tables():
            raise Exception("Error, not found. Please create this predictor first.")
        elif not model_name in mlflow_models:
            raise Exception(
                "Cannot connect with the model, it might not served. Please serve it with MLflow and try again.")

        model = self.connection.get_registered_model(model_name)
        model_info = self.storage.get('models')[model_name]
        return model_name, model, model_info['target'], model_info['model_url']

    def _call_model(self, df, model_url):
        resp = requests.post(model_url,
                             data=df.to_json(orient='records'),
                             headers={'content-type': 'application/json; format=pandas-records'})
        answer: List[object] = resp.json()

        predictions = pd.DataFrame({'prediction': answer})
        out = df.join(predictions)
        return out


if __name__ == '__main__':
    # TODO: turn this into tests

    if True:
        registered_model_name = 'nlp_kaggle3'  # already saved to mlflow local instance
        cls = MLflowIntegration('test_mlflow')
        print(cls.connect(
            mlflow_server_url='http://127.0.0.1:5001',  # for this test, serve at 5001 and served model at 5000
            model_registry_path='sqlite:////Users/Pato/Work/MindsDB/temp/experiments/BYOM/mlflow.db'))
        try:
            cls.run_native_query(f"DROP PREDICTOR {registered_model_name}")
        except:
            pass
        query = f"CREATE PREDICTOR {registered_model_name} PREDICT target USING url.predict='http://localhost:5000/invocations', format='mlflow', dtype_dict={{'text': 'rich_text', 'target': 'binary'}}"
        cls.run_native_query(query)
        print(cls.get_tables())
        print(cls.describe_table(f'{registered_model_name}'))

        # Tests with MySQL handler: JOIN
        from mindsdb.integrations.mysql_handler.mysql_handler.mysql_handler import MySQLHandler  # expose through parent init
        kwargs = {
            "host": "localhost",
            "port": "3306",
            "user": "root",
            "password": "root",
            "database": "test",
            "ssl": False
        }
        sql_handler_name = 'test_handler'
        data_table_name = 'train_escaped_csv' # 'tweet_sentiment_train'
        handler = MySQLHandler(sql_handler_name, **kwargs)
        assert handler.check_status()

        query = f"SELECT target from {registered_model_name} WHERE text='This is nice.'"
        parsed = cls.parser(query, dialect=cls.dialect)
        predicted = cls.select_query(parsed)

        query = f"SELECT tb.target as predicted, ta.target as real, tb.text from {sql_handler_name}.{data_table_name} AS ta JOIN {registered_model_name} AS tb LIMIT 10"
        parsed = cls.parser(query, dialect=cls.dialect)
        predicted = cls.join(parsed, handler)