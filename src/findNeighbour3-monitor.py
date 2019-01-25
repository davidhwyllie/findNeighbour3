#!/usr/bin/env python3
""" produces depictions of server activity, based on findNeighbour monitoring data """

# import libraries
import os
import sys
import logging
import warnings
import pymongo
import pandas as pd
import numpy as np
import pathlib
import sentry_sdk
import json
import time
import random
import dateutil.parser
import datetime
import unittest
from bokeh.embed import file_html
from bokeh.resources import CDN

# fn3 storage module
from mongoStore import fn3persistence
from depictStatus import MakeHumanReadable, DepictServerStatus

# startup
if __name__ == '__main__':

        # command line usage.  Pass the location of a config file as a single argument.
        # an example config file is default_test_config.json
               
        ############################ LOAD CONFIG ######################################
        print("findNeighbour3-dbmanager server .. reading configuration file.")

        max_batch_size = 100

        if len(sys.argv) == 2:
                configFile = sys.argv[1]
        else:
                configFile = os.path.join('..','config','default_test_config.json')
                warnings.warn("No config file name supplied ; using a configuration ('default_test_config.json') suitable only for testing, not for production. ")
   
        # open the config file
        try:
                with open(configFile,'r') as f:
                         CONFIG=f.read()

        except FileNotFoundError:
                raise FileNotFoundError("Passed one parameter, which should be a CONFIG file name; tried to open a config file at {0} but it does not exist ".format(sys.argv[1]))

        if isinstance(CONFIG, str):
                CONFIG=json.loads(CONFIG)	# assume JSON string; convert.

        # check CONFIG is a dictionary	
        if not isinstance(CONFIG, dict):
                raise KeyError("CONFIG must be either a dictionary or a JSON string encoding a dictionary.  It is: {0}".format(CONFIG))
        
        # check that the keys of config are as expected.

        required_keys=set(['IP', 'REST_PORT', 'DEBUGMODE', 'LOGFILE', 'MAXN_PROP_DEFAULT'])
        missing=required_keys-set(CONFIG.keys())
        if not missing == set([]):
                raise KeyError("Required keys were not found in CONFIG. Missing are {0}".format(missing))

        ########################### SET UP LOGGING #####################################  
        # create a log file if it does not exist.
        print("Starting logging")
        logdir = os.path.dirname(CONFIG['LOGFILE'])
        pathlib.Path(os.path.dirname(CONFIG['LOGFILE'])).mkdir(parents=True, exist_ok=True)

        # set up logger
        loglevel=logging.INFO
        if 'LOGLEVEL' in CONFIG.keys():
                if CONFIG['LOGLEVEL']=='WARN':
                        loglevel=logging.WARN
                elif CONFIG['LOGLEVEL']=='DEBUG':
                        loglevel=logging.DEBUG

        # configure logging object
        logger = logging.getLogger()
        logger.setLevel(loglevel)       
        file_handler = logging.FileHandler("dbmanager-{0}".format(os.path.basename(CONFIG['LOGFILE'])))
        formatter = logging.Formatter( "%(asctime)s | %(pathname)s:%(lineno)d | %(funcName)s | %(levelname)s | %(message)s ")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(logging.StreamHandler())
 

        # launch sentry if API key provided
        if 'SENTRY_URL' in CONFIG.keys():
                logger.info("Launching logger")
                sentry_sdk.init(CONFIG['SENTRY_URL'])

        # set min logging interval if not supplied
        if not 'SERVER_MONITORING_MIN_INTERVAL_MSEC' in CONFIG.keys():
               CONFIG['SERVER_MONITORING_MIN_INTERVAL_MSEC']=0

        #########################  CONFIGURE HELPER APPLICATIONS ######################


        ########################  START Operations ###################################
        logger.info("Preparing to produce visualisations")

        print("Connecting to backend data store at {0}".format(CONFIG['SERVERNAME']))
        try:
             PERSIST=fn3persistence(dbname = CONFIG['SERVERNAME'],
									connString=CONFIG['FNPERSISTENCE_CONNSTRING'],
									debug=CONFIG['DEBUGMODE'],
									server_monitoring_min_interval_msec = CONFIG['SERVER_MONITORING_MIN_INTERVAL_MSEC'])
        except Exception as e:
             logger.exception("Error raised on creating persistence object")
             if e.__module__ == "pymongo.errors":
                 logger.info("Error raised pertains to pyMongo connectivity")
                 raise
        dss1 = DepictServerStatus(logfile= CONFIG['LOGFILE'],
                                    server_url=CONFIG['IP'],
                                    server_port=CONFIG['REST_PORT'],
                                    server_description=CONFIG['DESCRIPTION'])
        print("Monitoring ..")
        while True:
            insert_data = PERSIST.recent_server_monitoring(selection_field="context|info|message", selection_string="About to insert", max_reported=100000)
            recent_data = PERSIST.recent_server_monitoring(selection_field="content|activity|whatprocess", selection_string="server", max_reported=1000)
            page_content = dss1.make_report(insert_data, recent_data)
            for item in page_content.keys():
                html = file_html(page_content[item], CDN, item)
                PERSIST.monitor_store(item, html)                
            print("Wrote output to database.  Waiting 2mins .. ")
            time.sleep(120)	# rerun in 2 minutes



