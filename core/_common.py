import os
import sys
import time
import yaml
import pickle
import string
import pathlib
import warnings
import numpy as np
import pandas as pd

# Suppress warnings
if not sys.warnoptions:
    warnings.simplefilter("ignore")

from efficient_apriori import apriori

# Workaround for Keras issue #1406
# "Using X backend." always printed to stdout #1406 
# https://github.com/keras-team/keras/issues/1406
stderr = sys.stderr
sys.stderr = open(os.devnull, 'w')
import keras
from keras import backend as kerasbackend
sys.stderr = stderr

import _utils as utils
import ServerSideExtension_pb2 as SSE

# Add Generated folder to module path
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PARENT_DIR, 'generated'))

class CommonFunction:
    """
    A class to implement common data science functions for Qlik.
    """
    
    # Counter used to name log files for instances of the class
    log_no = 0

    def __init__(self, request, context, path="../models/"):
        """
        Class initializer.
        :param request: an iterable sequence of RowData
        :param context:
        :param path: a directory path to look for persistent models
        :Sets up the model parameters based on the request
        """
               
        # Set the request, context and path variables for this object instance
        self.request = request
        self.context = context
        self.path = path
        self.logfile = None
    
    def association_rules(self):
        """
        Use an apriori algorithm to uncover association rules between items.
        """

        # Interpret the request data based on the expected row and column structure
        self._initiate(row_template = ['strData', 'strData', 'strData'],  col_headers = ['group', 'item', 'kwargs'])

        # Create a list of items for each group
        transactions = []

        # Iterate over each group and add a tuple of items to the list
        for group in self.request_df['group'].unique():
            transactions.append(tuple(self.request_df.item[self.request_df.group == group]))

        # Get the item sets and association rules from the apriori algorithm
        _, rules = apriori(transactions, **self.pass_on_kwargs)

        # Prepare the response
        response = []

        # for each rule get the left hand side and right hand side together with support, confidence and lift 
        for rule in sorted(rules, key=lambda rule: rule.lift, reverse=True):
            lhs = ", ".join(map(str, rule.lhs))
            rhs = ", ".join(map(str, rule.rhs))
            desc = "{0} -> {1}".format(lhs, rhs)
            response.append((desc, lhs, rhs, rule.support, rule.confidence, rule.lift))

        # if no association rules were found the parameters may need to be adjusted
        if len(response) == 0:
            err = "No association rules could be found. You may get results by lowering the limits imposed by " + \
                "the min_support and min_confidence parameters.\ne.g. by passing min_support=0.2|float in the arguments."
            raise Exception(err) 

        self.response_df = pd.DataFrame(response, columns=['rule', 'rule_lhs', 'rule_rhs', 'support', 'confidence', 'lift'])

        # Print the response dataframe to the logs
        if self.debug:
            self._print_log(4)

        # Send the reponse table description to Qlik
        self._send_table_description("apriori")
        
        return self.response_df

    def predict(self, load_script=False):
        """
        Return a prediction by using a pre-trained model.
        This method can be called from a chart expression or the load script in Qlik.
        The load_script flag needs to be set accordingly for the correct response.
        """

        # Interpret the request data based on the expected row and column structure
        row_template = ['strData', 'strData', 'strData']
        col_headers = ['model_name', 'n_features', 'kwargs']
        feature_col_num = 1

        # An additional key field column is expected if the call is made through the load script
        if load_script:
            row_template = ['strData', 'strData', 'strData', 'strData']
            col_headers = ['model_name', 'key', 'n_features', 'kwargs']
            feature_col_num = 2

        # Interpret the request data based on the expected row and column structure
        self._initiate(row_template, col_headers)

        # Set the name of the function to be called on the model
        # By default this is the predict function, but could be other functions such as 'predict_proba' if supported by the model
        prediction_func = 'predict'
        if 'return' in self.pass_on_kwargs:
            prediction_func = self.pass_on_kwargs['return']

        # Load the model
        self._get_model()

        # Prepare the input data
        if load_script:
            # Set the key column as the index
            self.request_df.set_index("key", drop=False, inplace=True)

        try:
            # Split the features provided as a string into individual columns
            self.X = pd.DataFrame([x[feature_col_num].split("|") for x in self.request_df.values.tolist()],\
                                        columns=self.features_df.loc[:,"name"].tolist(),\
                                        index=self.request_df.index)
        except AssertionError as ae:
            err = "The number of input columns do not match feature definitions. Ensure you are using the | delimiter and that the target is not included in your input to the prediction function."
            raise AssertionError(err) from ae

        # Convert the data types based on feature definitions 
        self.X = utils.convert_types(self.X, self.features_df, sort=False)

        # Apply preprocessing if required
        if self.prep is not None:
            self.X = self.prep.transform(self.X)

        # Generate predictions
        self.y = getattr(self.model, prediction_func)(self.X)

        # Prepare the response, catering for multiple outputs per sample
        # Multiple predictions for the same sample are separated by the pipe, i.e; | delimiter
        if len(self.y.shape) > 1 and self.y.shape[1] == 2:
            self.response_df = pd.DataFrame(["|".join(item) for item in self.y.astype(str)], columns=["result"], index=self.X.index)
        else:
            self.response_df = pd.DataFrame(self.y, columns=["result"], index=self.X.index)

        if load_script:
            # Add the key field column to the response
            self.response_df = self.request_df.join(self.response_df).drop(['n_features'], axis=1)
        
            # If the function was called through the load script we return a Data Frame
            self._send_table_description("predict")
            
            # Debug information is printed to the terminal and logs if the paramater debug = true
            if self.debug:
                self._print_log(4)
            
            return self.response_df
            
        # If the function was called through a chart expression we return a Series
        else:
            # Debug information is printed to the terminal and logs if the paramater debug = true
            if self.debug:
                self._print_log(4)
            
            return self.response_df.loc[:,'result']        

    def _initiate(self, row_template, col_headers):
        """
        Interpret the request data and setup execution parameters
        :
        :row_template: a list of data types expected in the request e.g. ['strData', 'numData']
        :col_headers: a list of column headers for interpreting the request data e.g. ['group', 'item']
        """
                
        # Create a Pandas DataFrame for the request data
        self.request_df = utils.request_df(self.request, row_template, col_headers)

        # Get the argument strings from the request dataframe
        kwargs = self.request_df.loc[0, 'kwargs']
        # Set the relevant parameters using the argument strings
        self._set_params(kwargs)

        # Print the request dataframe to the logs
        if self.debug:
            self._print_log(3)
    
    def _set_params(self, kwargs):
        """
        Set input parameters based on the request.
        :
        :For details refer to the GitHub project: https://github.com/nabeel-oz/qlik-py-tools
        """
        
        # Set default values which will be used if execution arguments are not passed
        
        # Default parameters:
        self.debug = False
        
        # If key word arguments were included in the request, get the parameters and values
        if len(kwargs) > 0:
            
            # Transform the string of arguments into a dictionary
            self.kwargs = utils.get_kwargs(kwargs)
            
            # Set the debug option for generating execution logs
            # Valid values are: true, false
            if 'debug' in self.kwargs:
                self.debug = 'true' == self.kwargs.pop('debug').lower()
                
                # Additional information is printed to the terminal and logs if the paramater debug = true
                if self.debug:
                    # Increment log counter for the class. Each instance of the class generates a new log.
                    self.__class__.log_no += 1

                    # Create a log file for the instance
                    # Logs will be stored in ..\logs\Common Functions Log <n>.txt
                    self.logfile = os.path.join(os.getcwd(), 'logs', 'Common Functions Log {}.txt'.format(self.log_no))

                    self._print_log(1)
            
            # Get the rest of the parameters, converting values to the correct data type
            self.pass_on_kwargs = utils.get_kwargs_by_type(self.kwargs) 
                          
        # Debug information is printed to the terminal and logs if the paramater debug = true
        if self.debug:
            self._print_log(2)
        
        # Remove the kwargs column from the request_df
        self.request_df = self.request_df.drop(['kwargs'], axis=1)

    def _get_model(self):
        """
        Load a model from disk.

        This function currently only supports sklearn models saved to disk using pickle.
        The version of Python and sklearn used to build the model must match this SSE.

        A YAML file describing the model as explained in this project's documentation needs to be placed at '../models/'

        e.g.
        ---
        path: '../pretrained/HR-Attrition-v1.pkl'
        type: sklearn
        features:
            department : str
            age : int
        ...
        """

        # Get the model name from the request dataframe
        model_name = self.request_df.loc[0, 'model_name']

        # Get model meta data from the YAML file
        try:
            with open(self.path + model_name + ".yaml", 'r') as stream:
                model_meta = yaml.safe_load(stream)
        except FileNotFoundError as fe:
            err = "Model definition file not found. A YAML file with the model path, type and features needs to be placed in ../models/"
            raise FileNotFoundError(err) from fe
        
        # Get model path, type, preprocessor (optional), and feature definitions
        model_path, model_type, model_features = model_meta['path'], model_meta['type'].lower(), model_meta['features']
        
        # Check that the model type is supported
        supported = ['sklearn', 'scikit-learn', 'keras']
        assert model_type in supported, "Unsupported model type: {}".format(model_meta['type'])

        # Get the preprocessor if required
        if 'preprocessor' in model_meta:
            prep_path = model_meta['preprocessor']
            self.prep = self._get_model_sklearn(prep_path)
        else:
            self.prep = None
        
        # Load the model
        if model_type in ['sklearn', 'scikit-learn']:
            self.model = self._get_model_sklearn(model_path)
        elif model_type in ['keras']:
            self.model = self._get_model_keras(model_path)
        
        self.name = model_name

        # Debug information is printed to the terminal and logs if the paramater debug = true
        if self.debug:
            self._print_log(6)
        
        # Set model feature names and data types
        self.features_df = pd.DataFrame([model_features.keys(), model_features.values()]).T
        self.features_df.columns = ['name', 'data_type']
        self.features_df = self.features_df.set_index('name', drop=False)
        self.features_df.loc[:,'variable_type'] = 'feature'

        # Debug information is printed to the terminal and logs if the paramater debug = true
        if self.debug:
            self._print_log(7)
    
    def _get_model_sklearn(self, model_path):
        """
        Load a pretrained scikit-learn pipeline from disk.
        The pipeline must have been saved in the pickle format.
        Versions for Python and scikit-learn should match the SSE.
        """

        # Add model directory to the system path
        self._add_model_path(model_path)

        # Load the saved pipeline from disk
        with open(model_path, 'rb') as file:
            model = pickle.load(file)
        
        return model

    def _get_model_keras(self, model_path):
        """
        Load a pretrained Keras model from disk.
        The model must have been saved in the HDF5 format.
        Versions for Python and Keras should match the SSE.
        """

        # Add model directory to the system path
        self._add_model_path(model_path)

        kerasbackend.clear_session()
         # Load the keras model architecture and weights from disk
        model = keras.models.load_model(model_path)
        model._make_predict_function()
        
        return model

    def _add_model_path(self, model_path):
        """
        Add the model's directory to the system path.
        """

        # Add model directory to the system path.
        model_dir = os.path.dirname(os.path.abspath(model_path))
        if model_dir not in sys.path:
            sys.path.append(model_dir)

    def _send_table_description(self, variant):
        """
        Send the table description to Qlik as meta data.
        Used when the SSE is called from the Qlik load script.
        """
        
        # Set up the table description to send as metadata to Qlik
        self.table = SSE.TableDescription()
        self.table.name = "SSE-Response"
        self.table.numberOfRows = len(self.response_df)

        # Set up fields for the table
        if variant == "apriori":
            self.table.fields.add(name="rule")
            self.table.fields.add(name="rule_lhs")
            self.table.fields.add(name="rule_rhs")
            self.table.fields.add(name="support", dataType=1)
            self.table.fields.add(name="confidence", dataType=1)
            self.table.fields.add(name="lift", dataType=1)
        elif variant == "predict":
            self.table.fields.add(name="model_name")
            self.table.fields.add(name="key")
            self.table.fields.add(name="prediction")
                
        # Debug information is printed to the terminal and logs if the paramater debug = true
        if self.debug:
            self._print_log(5)
            
        # Send table description
        table_header = (('qlik-tabledescription-bin', self.table.SerializeToString()),)
        self.context.send_initial_metadata(table_header)
    
    def _print_log(self, step):
        """
        Output useful information to stdout and the log file if debugging is required.
        :step: Print the corresponding step in the log
        """
        
        # Set mode to append to log file
        mode = 'a'

        if self.logfile is None:
            # Increment log counter for the class. Each instance of the class generates a new log.
            self.__class__.log_no += 1

            # Create a log file for the instance
            # Logs will be stored in ..\logs\SKLearn Log <n>.txt
            self.logfile = os.path.join(os.getcwd(), 'logs', 'Common Functions Log {}.txt'.format(self.log_no))
        
        if step == 1:
            # Output log header
            output = "\nCommonFunction Log: {0} \n\n".format(time.ctime(time.time()))
            # Set mode to write new log file
            mode = 'w'
                                
        elif step == 2:
            # Output the execution parameters to the terminal and log file
            output = "Execution parameters: {0}\n\n".format(self.kwargs) 
        
        elif step == 3:
            # Output the request data frame to the terminal and log file
            output = "REQUEST: {0} rows x cols\nSample Data:\n\n".format(self.request_df.shape)
            output += "{0}\n...\n{1}\n\n".format(self.request_df.head().to_string(), self.request_df.tail().to_string())
        
        elif step == 4:
            # Output the response data frame to the terminal and log file
            output = "RESPONSE: {0} rows x cols\nSample Data:\n\n".format(self.response_df.shape)
            output += "{0}\n...\n{1}\n\n".format(self.response_df.head().to_string(), self.response_df.tail().to_string())
        
        elif step == 5:
            # Output the table description if the call was made from the load script
            output = "TABLE DESCRIPTION SENT TO QLIK:\n\n{0} \n\n".format(self.table)
        
        elif step == 6:
            # Message when a pretrained model is loaded from path
            output = "Model '{0}' loaded from path.\n\n".format(self.name)
        
        elif step == 7:
            # Outpyt the feature definitions for the model
            output = "Feature definitions for model {0}:\n{1}\n\n".format(self.name, self.features_df.values.tolist())

        sys.stdout.write(output)
        with open(self.logfile, mode, encoding='utf-8') as f:
            f.write(output)

    def _print_exception(self, s, e):
        """
        Output exception message to stdout and also to the log file if debugging is required.
        :s: A description for the error
        :e: The exception
        """
        
        # Output exception message
        sys.stdout.write("\n{0}: {1} \n\n".format(s, e))
        
        if self.debug:
            with open(self.logfile,'a') as f:
                f.write("\n{0}: {1} \n\n".format(s, e))