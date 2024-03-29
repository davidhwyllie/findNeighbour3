#!/usr/bin/env python
""" 
A server providing relatedness information for bacterial genomes via a Restful API.

Implemented in pure Python3 3, it uses in-memory data storage backed by MongoDb.
It loads configuration from a config file, which must be set in production.

If no config file is provided, it will run in  'testing' mode with the  parameters
in default_test_config.json.  This expects a mongodb database to be running on
the default port on local host.  As a rough guide to the amount of space required in mongodb,
about 0.5MB of database is used per sequence, or about 2,000 sequences per GB.

If no config file is provided, it will run in  'testing' mode with the  parameters
in default_test_config.json.  This expects a mongodb database to be running on
the default port on local host.  As a rough guide to the amount of space required in mongodb,
about 0.5MB of database is used per sequence, or about 2,000 sequences per GB.

All internal modules, and the restful API, are covered by unit testing.
Unit testing can be achieved by:

# starting a test RESTFUL server
python3 findNeighbour3-server.py

# And then (e.g. in a different terminal) launching unit tests with
python3 -m unittest findNeighbour3-server

"""
 
# import libraries
import os
import sys
import requests
import json
import logging
import warnings
import datetime
import glob
import sys
import hashlib
import queue
import threading
import gc
import io
import pymongo
import pandas as pd
import numpy as np
import copy
import pathlib
import markdown
import codecs
import sentry_sdk
import matplotlib
import dateutil.parser
import argparse
import networkx as nx
from sentry_sdk import capture_message, capture_exception
from sentry_sdk.integrations.flask import FlaskIntegration


# flask
from flask import Flask, make_response, jsonify, Markup
from flask import request, abort, send_file
from flask_cors import CORS		# cross-origin requests are not permitted except for one resource, for testing

# logging
from logging.config import dictConfig

# utilities for file handling and measuring file size
import psutil

# reference based compression, storage and clustering modules
from NucleicAcid import NucleicAcid
from mongoStore import fn3persistence
from seqComparer import seqComparer		# import from seqComparer_mt for multithreading
from clustering import snv_clustering
from guidLookup import guidSearcher  # fast lookup of first part of guids

# network visualisation
from visualiseNetwork import snvNetwork

# server status visualisation
from depictStatus import MakeHumanReadable

# only used for unit testing
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import generic_nucleotide
import unittest
from urllib.parse import urlparse as urlparser
from urllib.parse import urljoin as urljoiner
import uuid
import time

class findNeighbour3():
	""" a server based application for maintaining a record of bacterial relatedness using SNP distances.
	
		The high level arrangement is that
		- This class interacts with in-memory sequences
		  [handled by the seqComparer class] and backends [fn3Persistance class] used by the server
		- methods in findNeighbour3() return native python3 objects.
		
		- a web server, currently flask, handles the inputs and outputs of this class
		- in particular, native python3 objects returned by this class are serialised by the Flask web server code.
		"""
		
	def __init__(self,CONFIG, PERSIST, on_startup_repack_memory_every = [None]):
		""" Using values in CONFIG, starts a server with CONFIG['NAME'] on port CONFIG['PORT'].

		CONFIG contains Configuration parameters relevant to the reference based compression system which lies
		at the core of the server.
			INPUTREF:       the path to fasta format reference file.
			EXCLUDEFILE:    a file containing the zero-indexed positions in the supplied sequences which should be ignored in all cases.
							Typically, this is because the software generating the mapped fasta file has elected not to call these regions,
							in any samples, e.g. because of difficulty mapping to these regions.
							Such regions can occupy up 5- 20% of the genome and it is important for efficient working of this software
							that these regions are supplied for exclusion on sequence loading.  Not doing so will slow loading, and markedly increase
							memory requirements, but will not alter the results produced.
			DEBUGMODE:      Controls operation of the server:

							DEBUGMODE =                                                              0       1        2
							Run server in production mode (errors logged, not returned to client)    Y       N        N
							Run server in debug mode (errors reported to client)                     N       Y        Y
							Create Database if it does not exist                                     Y       Y        Y
							Delete all data on startup                                               N       N        Y
							Enable /restart endpoint, which restarts empty server (for testing)      N       N        Y

			SERVERNAME:     the name of the server. used as the name of mongodb database which is bound to the server.
			FNPERSISTENCE_CONNSTRING: a valid mongodb connection string. if shard keys are set, the 'guid' field is suitable key.
							Note: if a FNPERSISTENCE_CONNSTRING environment variable is present, then the value of this will take precedence over any values in the config file.
							This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuraton file.
		
			MAXN_STORAGE:   The maximum number of Ns in the sequence <excluding those defined in > EXCLUDEFILE which should be indexed.
							Other files, e.g. those with all Ns, will be tagged as 'invalid'.  Although a record of their presence in the database
							is kept, they are not compared with other sequences.
			MAXN_PROP_DEFAULT: if the proportion not N in the sequence exceeds this, the sample is analysed, otherwise considered invalid.
			LOGFILE:        the log file used
			LOGLEVEL:		default logging level used by the server.  Valid values are DEBUG INFO WARNING ERROR CRITICAL
			SNPCEILING: 	links between guids > this are not stored in the database
			GC_ON_RECOMPRESS: if 'recompressing' sequences to a local reference, something the server does automatically, perform
							a full mark-and-sweep gc at this point.  This setting alters memory use and compute time, but not the results obtained.
			RECOMPRESS_FREQUENCY: if recompressable records are detected, recompress every RECOMPRESS_FREQ th detection (e.g. 5).
							Trades off compute time with mem usage.  This setting alters memory use and compute time, but not the results obtained.
							If zero, recompression is disabled.
			REPACK_FREQUENCY: see /docs/repack_frequency.md
			CLUSTERING:		a dictionary of parameters used for clustering.  In the below example, there are two different
							clustering settings defined, one named 'SNV12_ignore' and the other 'SNV12_include.
							{'SNV12_ignore' :{'snv_threshold':12, 'mixed_sample_management':'ignore', 'mixture_criterion':'p_value1', 'cutoff':0.001},
							 'SNV12_include':{'snv_threshold':12, 'mixed_sample_management':'include', 'mixture_criterion':'p_value1', 'cutoff':0.001}
							}
							Each setting is defined by four parameters:
							snv_threshold: clusters are formed if samples are <= snv_threshold from each other
							mixed_sample_management: this defines what happens if mixed samples are detected.
								Suppose there are three samples, A,B and M.  M is a mixture of A and B.
								A and B are > snv_threshold apart, but their distance to M is zero.
								If mixed_sample_management is
								'ignore', one cluster {A,B,M} is returned
								'include', two clusters {A,M} and {B,M}
								'exclude', three clusters are returns {A},{B},{C}
							mixture_criterion: sensible values include 'p_value1','p_value2','p_value3' but other output from  seqComparer._msa() is also possible.
								 these p-values arise from three different tests for mixtures.  Please see seqComparer._msa() for details.
							cutoff: samples are regarded as mixed if the mixture_criterion is less than or equal to this value.
			SENTRY_URL:  optional.  If provided, will launch link Sentry to the flask application using the API key provided.  See https://sentry.io for a description of this service. 

								Note: if a FN_SENTRY_URL environment variable is present, then the value of this will take precedence over any values in the config file.
								This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuraton file.
					LISTEN_TO:   optional.  If missing, will bind to localhost (only) on 127.0.0.1.  If present, will listen to requests from the IP stated.  if '0.0.0.0', the server will respond to all external requests.
		An example CONFIG is below:

		
		{			
		"DESCRIPTION":"A test server operating in ../unittest_tmp, only suitable for testing",
		"IP":"127.0.0.1",
		"INPUTREF":"../reference/TB-ref.fasta",
		"EXCLUDEFILE":"../reference/TB-exclude.txt",
		"DEBUGMODE":0,
		"SERVERNAME":"TBSNP",
		"FNPERSISTENCE_CONNSTRING":"mongodb://127.0.0.1",
		"MAXN_STORAGE":100000,
		"SNPCOMPRESSIONCEILING":250,
		"MAXN_PROP_DEFAULT":0.70,
		"LOGFILE":"../unittest_tmp/logfile.log",
		"LOGLEVEL":"INFO",
		"SNPCEILING": 20,
		"GC_ON_RECOMPRESS":1,
		"RECOMPRESS_FREQUENCY":5,
		"SERVER_MONITORING_MIN_INTERVAL_MSEC":0,
		"SENTRY_URL":"https://c******************@sentry.io/1******",
		"CLUSTERING":{'SNV12_ignore' :{'snv_threshold':12, 'mixed_sample_management':'ignore', 'mixture_criterion':'pvalue_1', 'cutoff':0.001},
					  'SNV12_include':{'snv_threshold':12, 'mixed_sample_management':'include', 'mixture_criterion':'pvalue_1', 'cutoff':0.001}
					 },
		"LISTEN_TO":"127.0.0.1"
		}

		Some of these settings are read when the server is first-run, stored in a database, and the server will not
		change the settings on re-start even if the config file is changed.  Examples are:
		SNPCEILING
		MAXN_PROP_DEFAULT
		EXCLUDEFILE
		INPUTREF
		CLUSTERING
		
		These settings cannot be changed because they alter the way that the data is stored; if you want to change
		the settings, the data will have to be re-loaded. 
		
		However, most other settings can be changed and will take effect on server restart.  These include:
		server location
		IP
		SERVERNAME
		REST_PORT
		LISTEN_TO (optional)
		
		internal logging	
		LOGFILE
		LOGLEVEL
		
		where the database connection binds to
		FNPERSISTENCE_CONNSTRING
		Note: if a FNPERSISTENCE_CONNSTRING environment variable is present, then the value of this will take precedence over any values in the config file.
		This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuration file.
		
		related to internal server memory management:
		GC_ON_RECOMPRESS
		RECOMPRESS_FREQUENCY
		SNPCOMPRESSIONCEILING
		
		related to what monitoring the server uses
		SERVER_MONITORING_MIN_INTERVAL_MSEC (optional)
		
		related to error handling
		SENTRY_URL (optional)
		Note: if a FN_SENTRY URL environment variable is present, then the value of this will take precedence over any values in the config file.
		This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuraton file.
		PERSIST is a storage object needs to be supplied.  The fn3Persistence class in mongoStore is one suitable object.
		PERSIST=fn3persistence(connString=CONFIG['FNPERSISTENCE_CONNSTRING'])

		"""
		
		# store the persistence object as part of the object
		self.PERSIST=PERSIST
		
		# check input
		if isinstance(CONFIG, str):
			self.CONFIG=json.loads(CONFIG)	# assume JSON string; convert.
		elif isinstance(CONFIG, dict):
			self.CONFIG=CONFIG
		else:
			raise TypeError("CONFIG must be a json string or dictionary, but it is a {0}".format(type(CONFIG)))
		
		# check it is a dictionary	
		if not isinstance(self.CONFIG, dict):
			raise KeyError("CONFIG must be either a dictionary or a JSON string encoding a dictionary.  It is: {0}".format(CONFIG))
		
		# check that the keys of config are as expected.
		required_keys=set(['IP','INPUTREF','EXCLUDEFILE','DEBUGMODE','SERVERNAME',
						   'FNPERSISTENCE_CONNSTRING', 'MAXN_STORAGE',
						   'SNPCOMPRESSIONCEILING', "SNPCEILING", 'MAXN_PROP_DEFAULT', 'REST_PORT',
						   'LOGFILE','LOGLEVEL','GC_ON_RECOMPRESS','RECOMPRESS_FREQUENCY', 'REPACK_FREQUENCY', 'CLUSTERING'])
		missing=required_keys-set(self.CONFIG.keys())
		if not missing == set([]):
			raise KeyError("Required keys were not found in CONFIG. Missing are {0}".format(missing))

		# the following keys are not stored in any database backend, as a server could be moved, i.e.
		# running on the same data but with different IP etc
		
		do_not_persist_keys=set(['IP',"SERVERNAME",'FNPERSISTENCE_CONNSTRING',
								 'LOGFILE','LOGLEVEL','REST_PORT',
								 'GC_ON_RECOMPRESS','RECOMPRESS_FREQUENCY', 'REPACK_FREQUENCY', 'SENTRY_URL', 'SERVER_MONITORING_MIN_INTERVAL_MSEC'])
				
		# determine whether this is a first-run situation.
		if self.PERSIST.first_run():
			self.first_run(do_not_persist_keys)

		# load global settings from those stored at the first run.
		if on_startup_repack_memory_every[0] is None:
			self.on_startup_repack_memory_every = 1e20		# not reachable
		else:
			self.on_startup_repack_memory_every = on_startup_repack_memory_every[0]
		cfg = self.PERSIST.config_read('config')
		
		# set easy to read properties from the config
		self.reference = cfg['reference']
		self.excludePositions = set(cfg['excludePositions'])
		self.debugMode = cfg['DEBUGMODE']
		self.maxNs = cfg['MAXN_STORAGE']
		self.snpCeiling = cfg['SNPCEILING']
		self.snpCompressionCeiling = cfg['SNPCOMPRESSIONCEILING']
		self.maxn_prop_default = cfg['MAXN_PROP_DEFAULT']
		self.clustering_settings = cfg['CLUSTERING']
		self.recompress_frequency = self.CONFIG['RECOMPRESS_FREQUENCY']
		self.repack_frequency = self.CONFIG['REPACK_FREQUENCY']
		self.gc_on_recompress = self.CONFIG['GC_ON_RECOMPRESS']
		
		## start setup
		self.write_semaphore = threading.BoundedSemaphore(1)        # used to permit only one process to INSERT at a time.
		
		# initialise nucleic acid analysis object
		self.objExaminer=NucleicAcid()
		
		# formatting utility
		self.mhr = MakeHumanReadable()
		
		# load in-memory sequences
		self.gs = guidSearcher()
		self._load_in_memory_data()
		
		print("findNeighbour3 is ready.")
	
	def _load_in_memory_data(self):
		""" loads in memory data into the seqComparer object from database storage """
		
		# set up clustering
		# while doing so, find a clustering strategy with the highest snv_threshold
		# we will use this for in-memory recompression.
		app.logger.info("findNeighbour3 is loading clustering data.")
		
		clustering_name_for_recompression = None
		max_snv_cutoff = 0
		self.clustering={}		# a dictionary of clustering objects, one per SNV cutoff/mixture management setting
		for clustering_name in self.clustering_settings.keys():
			json_repr = self.PERSIST.clusters_read(clustering_name)
			self.clustering[clustering_name] = snv_clustering(saved_result =json_repr)
			if self.clustering[clustering_name].snv_threshold > max_snv_cutoff:
				clustering_name_for_recompression = clustering_name
				max_snv_cutoff = self.clustering[clustering_name].snv_threshold
			app.logger.info("Loaded clustering {0} with SNV_threshold {1}".format(clustering_name, self.clustering[clustering_name].snv_threshold))
		
		if clustering_name_for_recompression is not None:
			app.logger.info("Will use clusters from pipeline {0} for in-memory recompression".format(clustering_name_for_recompression))
		else:
			app.logger.info("In memory recompression is not enabled as no clustering results found.")
			
		# ensure that clustering object is up to date.  clustering is an in-memory graph, which is periodically
		# persisted to disc.  It is possible that, if the server crashes/does a disorderly shutdown,
		# the clustering object which is persisted might not include all the guids in the reference compressed
		# database.  This situation is OK, because the clustering object will bring itself up to date when
		# the new guids and their links are loaded into it.

		# initialise seqComparer, which manages in-memory reference compressed data
		self.sc=seqComparer(reference=self.reference,
							maxNs=self.maxNs,
							snpCeiling= self.snpCeiling,
							debugMode=self.debugMode,
							excludePositions=self.excludePositions,
							snpCompressionCeiling = self.snpCompressionCeiling)
		app.logger.info("In-RAM data store set up; sequence comparison uses {0} threads".format(self.sc.cpuCount))
		
		# determine how many guids there in the database
		guids = self.PERSIST.refcompressedsequence_guids()
	
		self.server_monitoring_store(message='Starting load of sequences into memory from database')

		nLoaded = 0
		nRecompressed = 0
		# this object is just used for compression
		snvc = snv_clustering(snv_threshold=12, mixed_sample_management='ignore')		# compress highly similar sequences to a consensus
	
		for guid in guids:
			nLoaded+=1
			self.gs.add(guid)
			obj = self.PERSIST.refcompressedsequence_read(guid)
			self.sc.persist(obj, guid=guid)
			
			# recompression in ram is relatively slow.
			# to keep the server load fast, and memory usage low, we recompress after every n th sequence;
			# we use an existing clustering scheme (if present) in order to do this.
			# due to speed constraints, we don't do recompression unless there is an existing clustering scheme.
			
			# if the server is configured to compress memory and there is a clustering object available	
			if False:		# in ram recompression on reload is disabled until a storage solution is available
				if clustering_name_for_recompression is not None and self.recompress_frequency > 0:
					# every self.on_startup_repack_memory_every samples, or when the load is over, get the clusters
					if nLoaded % self.on_startup_repack_memory_every == 0 or nLoaded == len(guids):		# periodically recompress
						app.logger.info("Recompressing memory based on {2}.. {0}/{1} loaded".format(nLoaded, len(guids), clustering_name_for_recompression))
						nRecompressed = 0 	
						cl2g = self.clustering[clustering_name].clusters2guid()
						total_clusters = len(cl2g.keys())
						nClusters = 0
						for cl in cl2g.keys():
							nClusters +=1
							available_to_compress = []
							for guid in cl2g[cl]:
								if guid in self.sc.seqProfile.keys():		# it exists among records already loaded
									available_to_compress.append(guid)
							if len(available_to_compress)>3:
								# find a single guid from this cluster
								# and recompress relative to that.
								to_recompress = available_to_compress[0]	# the last sequence
								nRecompressed = nRecompressed + len(available_to_compress)
								if nClusters % 25 == 0:
									app.logger.info("Recompressed {0}/{1} clusters, {2}/{3} sequences ...".format(nClusters, total_clusters, nRecompressed,nLoaded))
								self.sc.compress_relative_to_consensus(to_recompress)
						
			if nLoaded % 500 ==0:
				if clustering_name_for_recompression is not None:
						app.logger.info("Loading {1} sequences from database .. ({0}).  Will repack memory every {2} sequences".format(self.sc.excluded_hash(),len(guids),self.on_startup_repack_memory_every))
				else:
						app.logger.info("Loading {1} sequences from database .. ({0}).".format(self.sc.excluded_hash(), len(guids)))
			
		print("findNeighbour3 has loaded {0} sequences from database; Recompressed {1}".format(len(guids),nRecompressed))
		
		app.logger.info("findNeighbour3 is checking clustering is up to date")
		self.update_clustering()
		#self.server_monitoring_store(message='Garbage collection.')		
		#gc.collect()		# free up ram		
		self.server_monitoring_store(message='Load from database complete.')
		#gc.disable()


	def reset(self):
		""" restarts the server, deleting any existing data """
		if not self.debugMode == 2:
			return		 # no action taken by calls to this unless debugMode ==2
		else:
			print("Deleting existing data and restarting")
			self.PERSIST._delete_existing_data()
			time.sleep(2) # let the database recover
			self._create_empty_clustering_objects()
			self._load_in_memory_data()

	def server_monitoring_store(self, message="No message supplied", guid=None):
		""" reports server memory information to store """
		sc_summary = self.sc.summarise_stored_items()
		db_summary = self.PERSIST.summarise_stored_items()
		mem_summary = self.PERSIST.memory_usage()
		self.PERSIST.server_monitoring_store(message=message, what='server', guid= guid, content={**sc_summary, **db_summary, **mem_summary})

	def first_run(self, do_not_persist_keys):
		""" actions taken on first-run only.
		Include caching results from CONFIGFILE to database, unless they are in do_not_persist_keys"""
		
		app.logger.info("First run situation: parsing inputs, storing to database. ")

		# create a config dictionary
		config_settings= {}
		
		# store start time 
		config_settings['createTime']= datetime.datetime.now()
		
		# store description
		config_settings['description']=self.CONFIG['DESCRIPTION']

		# store clustering settings
		self.clustering_settings=self.CONFIG['CLUSTERING']
		config_settings['clustering_settings']= self.clustering_settings
		
		# load the excluded bases
		excluded=set()
		if self.CONFIG['EXCLUDEFILE'] is not None:
			with open(self.CONFIG['EXCLUDEFILE'],'rt') as f:
				rows=f.readlines()
			for row in rows:
				excluded.add(int(row))

		app.logger.info("Noted {0} positions to exclude.".format(len(excluded)))
		config_settings['excludePositions'] = list(sorted(excluded))
		
		# load reference
		with open(self.CONFIG['INPUTREF'],'rt') as f:
			for r in SeqIO.parse(f,'fasta'):
				config_settings['reference']=str(r.seq)

		# create clusters objects
		app.logger.info("Setting up in-ram clustering objects..")
		self._create_empty_clustering_objects()
		
		# persist other config settings.
		for item in self.CONFIG.keys():
			if not item in do_not_persist_keys:
				config_settings[item]=self.CONFIG[item]
				
		res = self.PERSIST.config_store('config',config_settings)
		app.logger.info("First run actions complete.")
	
	
	def _create_empty_clustering_objects(self):
		""" create empty clustering objects """
		self.clustering = {}
		expected_clustering_config_keys = set(['snv_threshold',  'uncertain_base_type', 'mixed_sample_management', 'cutoff', 'mixture_criterion'])
		for clustering_name in self.clustering_settings.keys():
			observed = self.clustering_settings[clustering_name] 
			if not observed.keys() == expected_clustering_config_keys:
				raise KeyError("Got unexpected keys for clustering setting {0}: got {1}, expected {2}".format(clustering_name, observed, expected_clustering_config_keys))
			self.clustering[clustering_name] = snv_clustering(snv_threshold=observed['snv_threshold'] ,
															  mixed_sample_management=observed['mixed_sample_management'],
															  uncertain_base_type=observed['uncertain_base_type'])
			self.PERSIST.clusters_store(clustering_name, self.clustering[clustering_name].to_dict())
			app.logger.info("First run: Configured clustering {0} with SNV_threshold {1}".format( clustering_name, observed['snv_threshold']))

	def repack(self,guids=None):
		""" generates a smaller and faster representation in the persistence store
		for the guids in the list. optional"""
		if guids is None:
			guids = self.PERSIST.guids()  # all the guids
		for this_guid in guids:
			app.logger.debug("Repacking {0}".format(this_guid))
			self.PERSIST.guid2neighbour_repack(this_guid)
	
	def insert(self,guid,dna):
		""" insert DNA called guid into the server,
		persisting it in both RAM and on disc, and updating any clustering.
		"""
		
		# clean, and provide summary statistics for the sequence
		app.logger.info("Preparing to insert: {0}".format(guid))

		if not self.sc.iscachedinram(guid):                   # if the guid is not already there
			self.server_monitoring_store(message='About to insert',guid=guid)
			
			# prepare to insert
			self.objExaminer.examine(dna)  					  # examine the sequence
			cleaned_dna=self.objExaminer.nucleicAcidString.decode()
			refcompressedsequence =self.sc.compress(cleaned_dna)          # compress it and store it in RAM
			self.server_monitoring_store(message='Compression complete',guid=guid)

			self.sc.persist(refcompressedsequence, guid)			    # insert the DNA sequence into ram.
			self.server_monitoring_store(message='Stored to RAM',guid=guid)

			self.gs.add(guid)
			
			# construct links with everything existing existing at the time the semaphore was acquired.
			self.write_semaphore.acquire()				    # addition should be an atomic operation

			links={}			
			try:
				# this process reports links less than self.snpCeiling
				app.logger.debug("Finding links: {0}".format(guid))
				self.server_monitoring_store(message='Finding neighbours (mcompare - one vs. all)', guid=guid)
			
				res = self.sc.mcompare(guid)		# compare guid against all
				to_compress = 0
				for (guid1,guid2,dist,n1,n2,nboth, N1pos, N2pos, Nbothpos) in res: 	# all against all
					if not guid1==guid2:
						link = {'dist':dist,'n1':n1,'n2':n2,'nboth':nboth}
						if dist is not None:
							if link['dist'] <= self.snpCeiling:
								links[guid2]=link			
								to_compress +=1

				## now persist in database.  
				# we have considered what happens if database connectivity fails during the insert operations.
				app.logger.info("Persisting: {0}".format(guid))
				self.server_monitoring_store(message='Found neighbours; Persisting to disc', guid=guid)
			
				# if the database connectivity fails after this refcompressedseq_store has completed, then 
				# the 'document' will already exist within the mongo file store.
				# in such a case, a FileExistsError is raised.
				# we trap for such errors, logging a warning, but permitting continuing execution since
				# this is expected if refcompressedseq_store succeeds, but subsequent inserts fail.
				try:
					self.PERSIST.refcompressedseq_store(guid, refcompressedsequence)     # store the parsed object to database
				except FileExistsError:
					app.logger.warning("Attempted to refcompressedseq_store {0}, but it already exists.  This is expected only if database connectivity failed during a previous INSERT operation.  Such failures should be noted in earlier logs".format(guid))
				except Exception: 		# something else
					raise			# we don't want to trap other things
				self.server_monitoring_store(message='Stored to sequence disc', guid=guid)
			

				# annotation of guid will update if an existing record exists.  This is OK, and is acceptable if database connectivity failed during previous inserts
				self.PERSIST.guid_annotate(guid=guid, nameSpace='DNAQuality',annotDict=self.objExaminer.composition)						

				# addition of neighbours may cause neighbours to be entered more than once if database connectivity failed during previous inserts.
				# because of the way that extraction of links works, this does not matter, and duplicates will not be reported.
				self.PERSIST.guid2neighbour_add_links(guid=guid, targetguids=links)
				self.server_monitoring_store(message='Stored to links and annotations to disc', guid=guid)
	
			except Exception as e:
				app.logger.exception("Error raised on persisting {0}".format(guid))
				self.write_semaphore.release() 	# ensure release of the semaphore if an error is trapped

				# Rollback anything which could leave system in an inconsistent state
				# remove the guid from RAM is the only step necessary
				self.sc.remove(guid)	
				app.logger.info("Guid successfully removed from ram. {0}".format(guid))

				if e.__module__ == "pymongo.errors":
					app.logger.info("Error raised pertains to pyMongo connectivity")
					capture_exception(e)
					abort(503,e)		# the mongo server may be refusing connections, or busy.  This is observed occasionally in real-world use
				else:
					capture_exception(e)
					abort(500,e)		# some other kind of error

				
			# release semaphore
			self.write_semaphore.release()                  # release the write semaphore

			if self.recompress_frequency > 0:				
				if to_compress>= self.recompress_frequency and to_compress % self.recompress_frequency == 0:		# recompress if there are lots of neighbours, every self.recompress_frequency isolates
					self.server_monitoring_store(message='About to recompress', guid=guid)
					app.logger.debug("Recompressing: {0}".format(guid))
					self.sc.compress_relative_to_consensus(guid)
					self.server_monitoring_store(message='sample recompressed in RAM', guid=guid)

				#if self.gc_on_recompress==1:
				#	self.server_monitoring_store(message='About to GC', guid=guid)
					#gc.collect()
				#	self.server_monitoring_store(message='Finished GC', guid=guid)
	
			app.logger.info("Insert succeeded {0}".format(guid))

			# clean up guid2neighbour; this can readily be done post-hoc, if the process proves to be slow.
			# it is a mongodb reformatting operation which doesn't affect results.

			guids = list(links.keys())
			guids.append(guid)
		
			if self.repack_frequency>0:
				app.logger.info("Repacking around: {0}".format(guid))
				self.server_monitoring_store(message='Repacking database', guid=guid)
	
				if len(guids) % self.repack_frequency ==0:		# repack if there are repack_frequency-1 neighbours
					self.repack(guids)
				self.server_monitoring_store(message='Repacking over', guid=guid)
				
			# cluster
			app.logger.info("Clustering around: {0}".format(guid))
			self.server_monitoring_store(message='Starting clustering', guid=guid)
	
			self.update_clustering()
			self.server_monitoring_store(message='Finished clustering', guid=guid)
	
			return "Guid {0} inserted.".format(guid)		
		else:
			return "Guid {0} is already present".format(guid)
			app.logger.info("Already present, no insert needed: {0}".format(guid))
	
	def update_clustering(self, store=True):
		""" performs clustering on any samples within the persistence store which are not already clustered
			If Store=True, writes the clustered object to mongo."""
		
		# update clustering and re-cluster
		for clustering_name in self.clustering_settings.keys():
			
			# ensure that clustering object is up to date.
			
			# clustering is an in-memory graph, which is periodically
			# persisted to disc.
			
			# It is possible that, if the server crashes/does a disorderly shutdown,
			# the clustering object which is persisted might not include all the guids in the
			# reference compressed database.
			
			# This situation is OK, because the clustering object will
			# bring itself up to date when
			# the new guids and their links are loaded into it.
			
			guids = self.PERSIST.refcompressedsequence_guids()			# all guids processed and refernece compressed
			in_clustering_guids = self.clustering[clustering_name].guids()  # all clustered guids
			to_add_guids = guids - in_clustering_guids					# what we need to add
			remaining_to_add_guids = copy.copy(to_add_guids)				# we iterate until there's nothing left to add
			app.logger.info("Clustering graph {0} contains {2} guids out of {1}; updating.".format(clustering_name, len(guids), len(in_clustering_guids)))
			while len(remaining_to_add_guids)>0:
				to_add_guid = remaining_to_add_guids.pop()				# get the guid
				app.logger.debug("To {0} adding guid{1}".format(clustering_name, to_add_guid))

				links = self.PERSIST.guid2neighbours(to_add_guid, cutoff = self.clustering[clustering_name].snv_threshold, returned_format=3)['neighbours']	# and its links	
				app.logger.debug("To {0} links of guid {1} recovered {2}".format(clustering_name, to_add_guid, links))

				self.clustering[clustering_name].add_sample(to_add_guid, links)		# add it to the clustering db
				app.logger.debug("To {0} guid {1} has been added.".format(clustering_name, to_add_guid))

			in_clustering_guids = self.clustering[clustering_name].guids()  # all clustered guids

			app.logger.info("Clustering graph {0} contains {2}/{1} guids post update.".format(clustering_name, len(guids), len(in_clustering_guids)))

			# check any clusters to which to_add_guids have been added for mixtures.
			nMixed = 0
			guids_to_check = set()
			clusters_to_check = set()
			
			for to_add_guid in to_add_guids:
				guids_to_check.add(to_add_guid)								# then we need to check it and
				app.logger.debug("Clustering graph {0}; examining guid{1}".format(clustering_name, to_add_guid))

				for cluster in self.clustering[clustering_name].guid2clusters(to_add_guid):
					app.logger.debug("Clustering graph {0}; examining guid {1}, cluster is {2}".format(clustering_name, to_add_guid,cluster))

					clusters_to_check.add(cluster)						# everything else in the same cluster as it
			
			cl2guids = 	self.clustering[clustering_name].clusters2guid()	# dictionary allowing cluster -> guid lookup
			#app.logger.debug("Clustering graph {0};  recovered cl2guids {1}".format(clustering_name, cl2guids))

			for cluster in clusters_to_check:
				guids_for_msa = cl2guids[cluster]							# do msa on the cluster
				app.logger.debug("Checking cluster {0}; performing MSA on {1} samples".format(cluster,len(guids_for_msa)))
				msa = self.sc.multi_sequence_alignment(guids_for_msa, output='df',uncertain_base_type=self.clustering[clustering_name].uncertain_base_type)		#  a pandas dataframe; p_value tests mixed
				app.logger.debug("Multi sequence alignment is complete")

				if not msa is None:		# no alignment was made
					mixture_criterion = self.clustering_settings[clustering_name]['mixture_criterion']
					mixture_cutoff = self.clustering_settings[clustering_name]['cutoff']
					##################################################################################################
					## NOTE: query_criterion is EVAL'd by pandas.  This potentially a route to attack the server, but
					# to do so requires that you can edit either the CONFIG file (pre-startup) or the Mongodb in which
					# the config is stored post first-run
					##################################################################################################
					app.logger.debug("selecting mixed from msa of length {0}..".format(len(msa.index)))

					query_criterion = "{0} <= {1}".format(mixture_criterion,mixture_cutoff)					
					msa_mixed = msa.query(query_criterion)
					app.logger.debug("mixed samples selected from msa of length {0}..".format(len(msa_mixed.index)))
					
					# check the status of mixed samples in the cluster.
					mixed_status = {}
					n_mixed = 0
					for mixed_guid in msa_mixed.index:
						if self.clustering[clustering_name].is_mixed(mixed_guid)==True:   # if it's  known to be mixed
							n_mixed +=1
						else:
							mixed_status[mixed_guid]=True		# otherwise we set it as mixed;

					# if all the mixed samples are already assigned as such, we don't have to do anything.
					# otherwise:
					if not n_mixed == len(msa_mixed.index):		# all relevant samples are marked as mixed
						# recover all links in the cluster.
						app.logger.debug("There are mixed samples to update: currently vs required numbers {0} / {1}..".format(n_mixed, len(msa_mixed.index)))
								
						guid2neighbours = {}
						for guid in msa.index:
							app.logger.debug("Recovering links for guid {0}..".format(guid))

							guid2neighbours[guid]= self.PERSIST.guid2neighbours(guid, cutoff=self.clustering[clustering_name].snv_threshold, returned_format=3)['neighbours']

						app.logger.debug("Setting mixture status for {0}..".format(mixed_status))
							
						self.clustering[clustering_name].set_mixture_status(guid2similar_guids = guid2neighbours, change_guids = mixed_status)
						app.logger.debug("Setting mixture status complete..")
					else:
						pass
						app.logger.debug("Nothing to update")
				else:
					pass
					app.logger.debug("MSA was none")
					
			in_clustering_guids = self.clustering[clustering_name].guids()
			app.logger.info("Cluster {0} updated; now contains {1} guids. ".format(clustering_name, len(in_clustering_guids)))
			
			if store==True:
				self.PERSIST.clusters_store(clustering_name, self.clustering[clustering_name].to_dict())
				app.logger.debug("Cluster {0} persisted".format(clustering_name))
			
	def exist_sample(self,guid):
		""" determine whether the sample exists in RAM"""
		
		## this call measures presence on disc
		return self.PERSIST.guid_exists(guid)

	def server_time(self):
		""" returns the current server time """
		return {"server_name":self.CONFIG['SERVERNAME'], "server_time":datetime.datetime.now().isoformat()}

	def server_name(self):
		""" returns information about the server """
		return {"server_name":self.CONFIG['SERVERNAME'],
				"server_description":self.CONFIG['DESCRIPTION']
				}
	def server_config(self):
		""" returns the config file with which the server was launched
		
		This may be highly undesirable, and is only available in DEBUG mode.
		as it reveals the internal server architecture  including
		backend databases and perhaps connection strings with passwords.
		"""
		
		if self.debugMode==2:
			return self.CONFIG
		else:
			return None
	def server_nucleotides_excluded(self):
		""" returns the nucleotides excluded by the server """
		return {"exclusion_id":self.sc.excluded_hash(), "excluded_nt":list(self.sc.excluded)}
	
	def server_memory_usage(self, max_reported=None):
		""" reports recent server memory activity """
		if max_reported is None:
			max_reported =100		# a default
		return self.PERSIST.recent_server_monitoring(max_reported= max_reported)
	
	def neighbours_within_filter(self, guid, snpDistance, cutoff=0.85, returned_format=1):
		""" returns a list of guids, and their distances, by a sample quality cutoff	
			returns links either as
			format 1 [[otherGuid, distance]]
			or as
			format 2 [[otherGuid, distance, N_just1, N_just2, N_either]]
			or as
			format 3 [otherGuid, otherGuid2, otherGuid3]
			or as
			format 4 [{'guid':otherGuid, 'snv':distance}, {'guid':otherGuid2, 'snv':distance2}]
		"""

		# check the query is of good quality
		inScore = self.PERSIST.guid_quality_check(guid,float(cutoff))
		if inScore == None:
			raise KeyError("{0} not found".format(guid))	# that's an error, maybe should raise KeyError
		elif inScore == False:
			return []		# bad sequence; just to report no links

		# if it is of good quality, then we look for links
		idList=list()

		# gets the similar sequences from the database;
		retVal = self.PERSIST.guid2neighbours(guid=guid, cutoff=snpDistance, returned_format=returned_format)
		
		# run a quality check on the things our sample is like.
		# extract the matching guids, independent of the format requested.
		sampleList=retVal['neighbours']
		idList=[]
		for sa in sampleList:
			if isinstance(sa, list):
				idList.append(sa[0])		# add the guid
			elif isinstance(sa, str):
				idList.append(sa)
			elif isinstance(sa, dict):
				idList.append(sa['guid'])
			else:
				raise TypeError("Unknown format returned {0} {1}".format(type(sa),sampleList))
		
		guid2qual=self.PERSIST.guid2quality(idList)
					  
		# Filter to get good matching guids
		goodGuids=set()
		cutoff=float(cutoff)
		for guid in guid2qual.keys():
			if guid2qual[guid]>=cutoff:
				goodGuids.add(guid)
		
		# note one could put a filter to identify samples based on Ns here: these statistics are return in the sampleList
		
		# assemble output by filtering sampleList
		finalOutput = list()
		for sa in sampleList:
			if isinstance(sa, list):
				guid = sa[0]
			elif isinstance(sa, str):
				guid = sa
			elif isinstance(sa, dict):
				guid = sa['guid']
		
			if guid in goodGuids:
				finalOutput.append(sa)
				
		return finalOutput
	
	def get_all_guids(self):
		return self.PERSIST.guids()
	
	def guids_with_quality_over(self,cutoff=0.66):
		rs=self.PERSIST.guid2propACTG_filtered(float(cutoff))
		if rs==None:
			return []
		else:
			return list(rs.keys())
		
	def get_all_guids_examination_time(self):
		res = self.PERSIST.guid2ExaminationDateTime()
		# isoformat all the keys, as times are not json serialisable
		retDict = res
		for key in retDict:
			retDict[key]=retDict[key].isoformat()
		return(retDict)
	
	def get_all_annotations(self):
		return self.PERSIST.guid_annotations()

	def get_one_annotation(self, guid):
		return self.PERSIST.guid_annotation(guid)
		
	def query_get_detail(self, sname1, sname2):
		""" gets detail on the comparison of a pair of samples.  Computes this on the fly """
		ret = self.sc.query_get_detail(sname1,sname2)
		return(ret)

	def sequence(self, guid):
		""" gets masked sequence for the guid, in format sequence|fasta """
		if not self.sc.iscachedinram(guid):
			return None
		try:		
			seq = self.sc.uncompress(self.sc.seqProfile[guid])
			return {'guid':guid, 'invalid':0,'comment':'Masked sequence, as stored','masked_dna':seq}
		except ValueError:
				return {'guid':guid, 'invalid':1,'comment':'No sequence is available, as invalid sequences are not stored'}
			
# default parameters for unit testing only.
RESTBASEURL   = "http://127.0.0.1:5020"
ISDEBUG = True
LISTEN_TO = '127.0.0.1'		# only local addresses

# initialise Flask 
app = Flask(__name__)
CORS(app)	# allow CORS
app.logger.setLevel(logging.DEBUG)

			
def isjson(content):
		""" returns true if content parses as json, otherwise false. used by unit testing. """
		try:
			x = json.loads(content.decode('utf-8'))
			return True
 
		except json.decoder.JSONDecodeError:
			return False

def tojson(content):
	""" json dumps, formatting dates as isoformat """
	def converter(o):
		if isinstance(o, datetime.datetime):
			return o.isoformat()
		else:
			return json.JSONEncoder.default(o)
	return(json.dumps(content, default=converter))

# --------------------------------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(error):
	json_err = jsonify({'error': 'Not found (custom error handler for mis-routing)'})
	return make_response(json_err, 404)
# --------------------------------------------------------------------------------------------------
 
@app.teardown_appcontext
def shutdown_session(exception=None):
	fn3.PERSIST.closedown()		# close database connection

def do_GET(relpath):
	""" makes a GET request  to relpath.
		Used for unit testing.   """
	
	url = urljoiner(RESTBASEURL, relpath)
	print("GETing from: {0}".format(url))

	session = requests.Session()
	session.trust_env = False

	# print out diagnostics
	print("About to GET from url {0}".format(url))
	response = session.get(url=url, timeout=None)

	print("Result:")
	print("code: {0}".format(response.status_code))
	print("reason: {0}".format(response.reason))
	try:     
		print("text: {0}".format(response.text[:100]))
	except UnicodeEncodeError:
		# which is what happens if you try to display a gz file as text, which it isn't
		print("Response cannot be coerced to unicode ? a gz file.  The response content had {0} bytes.".format(len(response.text)))
		print("headers: {0}".format(response.headers))

	session.close()
	return(response)

def do_POST(relpath, payload):
	""" makes a POST request  to relpath.
		Used for unit testing.
		payload should be a dictionary"""
	
	url = urljoiner(RESTBASEURL, relpath)

	# print out diagnostics
	print("POSTING to url {0}".format(url))
	if not isinstance(payload, dict):
		raise TypeError("not a dict {0}".format(payload))
	response = requests.post(url=url, data=payload)

	print("Result:")
	print("code: {0}".format(response.status_code))
	print("reason: {0}".format(response.reason))
	print("content: {0}".format(response.content))
		
	return(response)

def render_markdown(md_file):
	""" render markdown as html
	"""
	with codecs.open(md_file, mode="r", encoding="utf-8") as f:
		text = f.read()
		html = markdown.markdown(text, extensions = ['tables'])
	return html

@app.route('/', methods=['GET'])
def routes():
	""" returns server info page
	"""
	routes_file = os.path.join("..","doc","rest-routes.md")
	return make_response(render_markdown(routes_file))

@app.route('/ui/info', methods=['GET'])
def server_info():
	""" returns server info page
	"""
	routes_file = os.path.join("..","doc","serverinfo.md")
	return make_response(render_markdown(routes_file))

@app.route('/api/v2/raise_error/<string:component>/<string:token>', methods=['GET'])
def raise_error(component, token):
	""" * raises an error internally.  Can be used to test error logging.  Disabled unless in debug mode.

	/api/v2/raise_error/*component*/*token*/

	Valid values for component are:
	main - raise error in main code
	persist - raise in PERSIST object
	clustering - raise in clustering
	seqcomparer - raise in seqcomparer.
	"""

	if not fn3.debugMode == 2:
		# if we're not in debugMode==2, then this option is not allowed
		abort(404, 'Calls to /raise_error are only allowed with debugMode == 2' )

	if component == 'main':
		raise ZeroDivisionError(token)
	elif component == 'clustering':
		clustering_names = list(fn3.clustering_settings.keys())

		if len(clustering_names)==0:
			self.fail("no clustering settings defined; cannot test error generation in clustering")
		else:
			clustering_name = clustering_names[0]
			fn3.clustering_settings[clustering_name].raise_error(token)
	elif component == 'seqcomparer':
		fn3.sc.raise_error(token)
	elif component == 'persist':
		fn3.PERSIST.raise_error(token)
	else:
		raise KeyError("Invalid component called.  Allowed: main;persist;clustering;seqcomparer.")

@unittest.skip("skipped; known issue with error handling within flask")		
class test_raise(unittest.TestCase):
	""" tests route /api/v2/reset
	
	Note: this test currently fails, and has been disabled.
	It appears that (at least as currently configured) errors raised during
	Flask execution are not logged to app.logger.
	
	This is unexpected; the errors raised are printed to STDERR and
	are also logged using Sentry, if configured.
	
	The logger is working, and explicit calls to app.logger.exception() within try/except blocks
	do log.
	
	This remains an unresolved issue.
	"""
	def runTest(self):
		
		# get the server's config - requires that we're running in debug mode
		relpath = "/api/v2/server_config"
		try:
			res = do_GET(relpath)
		except requests.exceptions.HTTPError:
			self.fail("Could not read config. This unit test requires a server in debug mode")
			
		self.assertTrue(isjson(content = res.content))

		config_dict = json.loads(res.content.decode('utf-8'))
		try:
			logfile = config_dict['LOGFILE']
		except KeyError:
			self.fail("No LOGFILE element in config dictionary, as obtained from the server.")
			
		for error_at in ['main','persist','clustering','seqcomparer']:
			guid = uuid.uuid4().hex

			token = "TEST_ERROR_in_{0}_#_{1}".format(error_at, guid)
			relpath = "/api/v2/raise_error/{0}/{1}".format(error_at, token)

			res = do_GET(relpath)
	
			if not os.path.exists(logfile):
				self.fail("No logfile {0} exists.  This test only works when the server is on localhost, and debugmode is 2".format(logfile))
			else:
				with open(logfile, 'rt') as f:
					txt = f.read()
					if not token in txt:
						print("NOT LOGGED: ***** {0} <<<<<<".format(txt[-200:]))
						self.fail("Error was not logged {0}".format(error_at))
	
def construct_msa(guids, output_format, what):
	
	""" constructs multiple sequence alignment for guids
		and returns in one of 'fasta' 'json-fasta', 'html', 'json' or 'json-records' format.
		
		what is one of 'N','M','N_or_M'
	
	"""
	res = fn3.sc.multi_sequence_alignment(guids, output='df_dict', uncertain_base_type=what)
	df = pd.DataFrame.from_dict(res,orient='index')
	html = df.to_html()
	fasta= ""
	for guid in df.index:
		fasta=fasta + ">{0}\n{1}\n".format(guid, df.loc[guid,'aligned_seq'])
		
	if output_format == 'fasta':
		return make_response(fasta)
	elif output_format == 'json-fasta':
		return make_response(json.dumps({'fasta':fasta}))
	elif output_format == 'html':
		return make_response(html)
	elif output_format == 'json':
		return make_response(json.dumps(res))
	elif output_format == 'json-records':
		if len(df.index)>0:
			df['guid'] = df.index
		return make_response(df.to_json(orient='records'))
	
@app.route('/api/v2/reset', methods=['POST'])
def reset():
	""" deletes any existing data from the server """
	if not fn3.debugMode == 2:
		# if we're not in debugMode==2, then this option is not allowed
		abort(404, 'Calls to /reset are only allowed with debugMode == 2' )
	else:
		fn3.reset()
		return make_response(json.dumps({'message':'reset completed'}))
class test_reset(unittest.TestCase):
	""" tests route /api/v2/reset
	"""
	def runTest(self):
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids
		
		guid_to_insert = "guid_{0}".format(n_pre+1)
		
		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)
		
		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_post = len(json.loads(str(res.text)))		# get all the guids
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_post_reset = len(json.loads(str(res.text)))		# get all the guids
			
		self.assertTrue(n_post>0)
		self.assertTrue(n_post_reset==0)

@app.route('/api/v2/monitor', methods=['GET'])
@app.route('/api/v2/monitor/<string:report_type>', methods=['GET'])
def monitor(report_type = 'Report' ):
	""" returns an html/bokeh file, generated by findNeighbour3-monitor,
	and stored in a database. If not report_type is specified, uses 'Report',
	which is the name of the default report produced by findNeighbour3-monitor"""
	
	html = fn3.PERSIST.monitor_read(report_type)
	if html is None:
		html = "No report called {0} is available.  Check that findNeighbour3-monitor.py is running.".format(report_type)
	return html
@app.route('/api/v2/clustering/<string:clustering_algorithm>/<int:cluster_id>/network',methods=['GET'])
@app.route('/api/v2/clustering/<string:clustering_algorithm>/<int:cluster_id>/minimum_spanning_tree',methods=['GET'])
def cl2network(clustering_algorithm, cluster_id):
	""" produces a cytoscape.js compatible graph from a cluster ,
	either from the network (comprising all edges < snp cutoff)
	or as a minimal spanning tree.
	"""
	# validate input
	try:
		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)		
	except KeyError:
		# no clustering algorithm of this type
		return make_response(tojson("no clustering algorithm {0}".format(clustering_algorithm)), 404)
		
	# check guids
	df = pd.DataFrame.from_records(res)

	# check guids
	df = pd.DataFrame.from_records(res)
	
	if len(df.index)==0:
		return make_response(
								tojson(
									{'success':0, 'message':'No samples exist for that cluster'}
								)
							)
	else:
		df = df[df["cluster_id"]==cluster_id]		# only if there are records
		missing_guids = []
		guids = sorted(df['guid'].tolist())
					
		# data validation complete.  construct outputs
		snv_threshold = fn3.clustering_settings[clustering_algorithm]['snv_threshold']
		snvn = snvNetwork(snv_threshold = snv_threshold)
		E=[]
		for guid in guids:
			is_mixed = int(fn3.clustering[clustering_algorithm].is_mixed(guid))
			snvn.G.add_node(guid, is_mixed=is_mixed)     
		for guid in guids:
			res = fn3.PERSIST.guid2neighbours(guid, cutoff=snv_threshold, returned_format=1)
			for (guid2, snv) in res['neighbours']:
				if guid2 in guids:		# don't link outside the cluster
					E.append((guid,guid2))
					snvn.G.add_edge(guid, guid2, weight=snv, snv=snv)
				
		if request.base_url.endswith('/minimum_spanning_tree'):
			snvn.G = nx.minimum_spanning_tree(snvn.G)
			retVal =snvn.network2cytoscapejs()
			retVal['message']='{0} cluster #{1}. Minimum spanning tree is shown.  Red nodes are mixed.'.format(clustering_algorithm,cluster_id)
		else:
			retVal = snvn.network2cytoscapejs()
			retVal['message']='{0} cluster #{1}. Network of all edges < cutoff shown.  Red nodes are mixed.'.format(clustering_algorithm,cluster_id)
		retVal['success']=1
		return make_response(tojson(retVal))

class test_cl2network(unittest.TestCase):
	"""  tests return of a change_id number """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		# add four samples, two mixed
		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
		guids_inserted = list()
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		for i in range(1,4):
			
			seq = originalseq
			if i % 2 ==0:
				is_mixed = True
				guid_to_insert = "mixed_{0}".format(n_pre+i)
			else:
				is_mixed = False
				guid_to_insert = "nomix_{0}".format(n_pre+i)	
			# make i mutations at position 500,000
			
			offset = 500000
			for j in range(i):
				mutbase = offset+j
				ref = seq[mutbase]
				if is_mixed == False:
					if not ref == 'T':
						seq[mutbase] = 'T'
					if not ref == 'A':
						seq[mutbase] = 'A'
				if is_mixed == True:
						seq[mutbase] = 'N'					
			seq = ''.join(seq)
			guids_inserted.append(guid_to_insert)			
		
			relpath = "/api/v2/insert"
			res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
			self.assertEqual(res.status_code, 200)

		relpath = "/api/v2/clustering/SNV12_ignore/cluster_ids"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(res.text)
		# plot the cluster with the highest clusterid
		relpath = '/api/v2/clustering/SNV12_ignore/{0}/network'.format(max(retVal))
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		jsonresp = json.loads(str(res.text))
		self.assertTrue(isinstance(jsonresp, dict))
		self.assertTrue('elements' in jsonresp.keys())

		# plot the cluster with the highest clusterid
		res = None
		relpath = '/api/v2/clustering/SNV12_ignore/{0}/minimum_spanning_tree'.format(max(retVal))
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		jsonresp = json.loads(str(res.text))
		self.assertTrue(isinstance(jsonresp, dict))
		self.assertTrue('elements' in jsonresp.keys())
		#for item in jsonresp['elements']:
		#	print(item)
	
@app.route('/api/v2/multiple_alignment/guids', methods=['POST'])
def msa_guids():
	""" performs a multiple sequence alignment on a series of POSTed guids,
	delivered in a dictionary, e.g.
	{'guids':'guid1;guid2;guid3',
	'output_format':'json'}
	
	Valid values for output_format are:
	json
	json-records
	html
	json-fasta
	fasta
	
	Valid values for what are
	N
	M
	N_or_M
	"""

	# validate input
	request_payload = request.form.to_dict()
	if 'output_format' in request_payload.keys() and 'guids' in request_payload.keys():
		guids = request_payload['guids'].split(';')		# coerce both guid and seq to strings
		output_format= request_payload['output_format']
		if 'what' in request_payload.keys():
			what = request_payload['what']
		else:
			what = 'N'		# default to N
		if not what in ['N','M','N_or_M']:
			abort(404, 'what must be one of N M N_or_M, not {0}'.format(what))
		if not output_format in ['html','json','fasta', 'json-fasta', 'json-records']:
			abort(404, 'output_format must be one of html, json, json-records or fasta not {0}'.format(output_format))
	else:
		abort(501, 'output_format and guids are not present in the POSTed data {0}'.format(data_keys))
	
	# check guids
	missing_guids = []
	for guid in sorted(guids):
		try:
			result = fn3.exist_sample(guid)
		except Exception as e:
			capture_exception(e)
			abort(500, e)
		if not result is True:
			missing_guids.append(guid)
	
	if len(missing_guids)>0:
		capture_message("asked to perform multiple sequence alignment with the following missing guids: {0}".format(missing_guids))		
		abort(501, "asked to perform multiple sequence alignment with the following missing guids: {0}".format(missing_guids))
	
	# data validation complete.  construct outputs
	return construct_msa(guids, output_format, what)


class test_msa_2(unittest.TestCase):
	""" tests route /api/v2/multiple_alignment/guids, with additional samples.
	"""
	def runTest(self):
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
		inserted_guids = ['guid_ref']
		seq="".join(originalseq)
		res = do_POST("/api/v2/insert", payload = {'guid':'guid_ref','seq':seq})


		for k in range(0,1):
			# form one clusters
			for i in range(0,3):
				guid_to_insert = "msa2_{1}_guid_{0}".format(n_pre+k*100+i,k)
				inserted_guids.append(guid_to_insert)
				muts = 0
				seq = originalseq			
				# make i mutations at position 500,000
				if k==1:
					for j in range(1000000,1000100):		# make 100 mutants at position 1m
						mutbase = offset+j
						ref = seq[mutbase]
						if not ref == 'T':
							seq[mutbase] = 'T'
						if not ref == 'A':
							seq[mutbase] = 'A'
						muts+=1
	
				offset = 500000
				for j in range(i):
					mutbase = offset+j
					ref = seq[mutbase]
					if not ref == 'T':
						seq[mutbase] = 'T'
					if not ref == 'A':
						seq[mutbase] = 'A'
					muts+=1
				seq = ''.join(seq)
							
				print("Adding TB sequence {2} of {0} bytes with {1} mutations relative to ref.".format(len(seq), muts, guid_to_insert))
				self.assertEqual(len(seq), 4411532)		# check it's the right sequence
		
				relpath = "/api/v2/insert"
				res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
				self.assertTrue(isjson(content = res.content))
				info = json.loads(res.content.decode('utf-8'))
				self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
	
		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertTrue(b"</table>" in res.content)
		
		
		payload = {'guids':';'.join(inserted_guids),'output_format':'json'}
		res = do_POST(relpath, payload=payload)
		self.assertTrue(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertFalse(b"</table>" in res.content)
		d = json.loads(res.content.decode('utf-8'))
		not_present = set(inserted_guids) - set(d.keys())
		self.assertEqual(not_present, set())

		payload = {'guids':';'.join(inserted_guids),'output_format':'json-records'}
		res = do_POST(relpath, payload=payload)
		self.assertTrue(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertFalse(b"</table>" in res.content)
		d = json.loads(res.content.decode('utf-8'))
		
		payload = {'guids':';'.join(inserted_guids),'output_format':'fasta'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)

		payload = {'guids':';'.join(inserted_guids),'output_format':'json-fasta'}
		res = do_POST(relpath, payload=payload)
		self.assertTrue(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(res.content.decode('utf_8'))
		self.assertTrue(isinstance(retVal, dict))
		self.assertEqual(set(retVal.keys()), set(['fasta']))

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html', 'what':'N'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertTrue(b"</table>" in res.content)

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html', 'what':'M'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertTrue(b"</table>" in res.content)

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html', 'what':'N_or_M'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertTrue(b"</table>" in res.content)

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'json-records', 'what':'N'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)
		self.assertEqual(df.loc[df.index[0],'what_tested'],'N')
		
		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'json-records', 'what':'M'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)
		self.assertEqual(df.loc[df.index[0],'what_tested'],'M')

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'json-records', 'what':'N_or_M'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)		
		self.assertEqual(df.loc[df.index[0],'what_tested'],'N_or_M')

		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'json-records', 'what':'N'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)		
		self.assertEqual(df.loc[df.index[0],'what_tested'],'N')
								 
		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html', 'what':'X'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 404)

				
		relpath = "/api/v2/clustering/SNV12_ignore/guids2clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal, list))
		res = json.loads(res.content.decode('utf-8'))
		cluster_id=None

		for item in res:
			if item['guid'] in inserted_guids:
				cluster_id = item['cluster_id']
		#print("Am examining cluster_id",cluster_id)
		self.assertTrue(cluster_id is not None)
		relpath = "/api/v2/multiple_alignment_cluster/SNV12_ignore/{0}/json-records".format(cluster_id)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)		
		self.assertEqual(df.loc[df.index[0],'what_tested'],'N')

		relpath = "/api/v2/multiple_alignment_cluster/SNV12_include_M/{0}/json-records".format(cluster_id)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)		
		self.assertEqual(df.loc[df.index[0],'what_tested'],'M')
	
		
		relpath = "/api/v2/multiple_alignment_cluster/SNV12_ignore/{0}/fasta".format(cluster_id)
		res = do_GET(relpath)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)

@app.route('/api/v2/multiple_alignment_cluster/<string:clustering_algorithm>/<int:cluster_id>/<string:output_format>',methods=['GET'])
def msa_guids_by_cluster(clustering_algorithm, cluster_id, output_format):
	""" performs a multiple sequence alignment on the contents of a cluster
	
	Valid values for format are:
	json
	json-records
	fasta
	html
	"""
	
	# validate input
	try:
		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)		
	except KeyError:
		# no clustering algorithm of this type
		return make_response(tojson("no clustering algorithm {0}".format(clustering_algorithm)), 404)
		
	if not output_format in ['html','json','json-records','fasta','json-fasta']:
		abort(501, 'output_format must be one of html, json, json-records fasta or json-fasta not {0}'.format(output_format))

	# check guids
	df = pd.DataFrame.from_records(res)

	if len(df.index)==0:
		return make_response(
								json.dumps(
									{'status':'No samples exist for that cluster'}
								)
							)
	else:
		df = df[df["cluster_id"]==cluster_id]
		missing_guids = []
		guids = []
		for guid in sorted(df['guid'].tolist()):
			try:
				result = fn3.exist_sample(guid)
			except Exception as e:
				capture_exception(e)
				abort(500, e)
			if not result is True:
				missing_guids.append(guid)
			else:
				guids.append(guid)
		
		if len(missing_guids)>0:
			abort(501, "asked to perform multiple sequence alignment with the following missing guids: {0}".format(missing_guids))
			
		# data validation complete.  construct outputs
		return construct_msa(guids, output_format, what=fn3.clustering[clustering_algorithm].uncertain_base_type)


class test_msa_1(unittest.TestCase):
	""" tests route /api/v2/multiple_alignment/guids, with additional samples.
	"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={}) 		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids
		print("There are {0} existing samples".format(n_pre))
		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
		inserted_guids = []			
		for i in range(0,3):
			guid_to_insert = "msa1_guid_{0}".format(n_pre+i)
			inserted_guids.append(guid_to_insert)
			
			seq = originalseq			
			# make i mutations at position 500,000
			offset = 500000
			for j in range(i):
				mutbase = offset+j
				ref = seq[mutbase]
				if not ref == 'T':
					seq[mutbase] = 'T'
				if not ref == 'A':
					seq[mutbase] = 'A'
			seq = ''.join(seq)
						
			print("Adding TB sequence {2} of {0} bytes with {1} mutations relative to ref.".format(len(seq), i, guid_to_insert))
			self.assertEqual(len(seq), 4411532)		# check it's the right sequence
	
			relpath = "/api/v2/insert"
			res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
			self.assertEqual(res.status_code, 200)
		
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
	
		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(inserted_guids),'output_format':'html'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertTrue(b"</table>" in res.content)
		
		payload = {'guids':';'.join(inserted_guids),'output_format':'json'}
		res = do_POST(relpath, payload=payload)
		self.assertTrue(isjson(res.content))
		self.assertEqual(res.status_code, 200)
		self.assertFalse(b"</table>" in res.content)
		d = json.loads(res.content.decode('utf-8'))
		self.assertEqual(set(d.keys()), set(inserted_guids))

		self.assertEqual(len(d.keys()), 3)		# should create a cluster of three
		
		payload = {'guids':';'.join(inserted_guids),'output_format':'fasta'}
		res = do_POST(relpath, payload=payload)
		self.assertFalse(isjson(res.content))
		self.assertEqual(res.status_code, 200)


@app.route('/api/v2/server_config', methods=['GET'])
def server_config():
	""" returns server configuration.

		returns the config file with which the server was launched.
		This may be highly undesirable,
		as it reveals the internal server architecture  including
		backend databases and perhaps connection strings with passwords.

	"""
	res = fn3.server_config()
	if res is None:		# not allowed to see it
		return make_response(tojson({'NotAvailable':"Endpoint is only available in debug mode"}), 404)
	else:
		return make_response(tojson(CONFIG))

class test_server_config(unittest.TestCase):
	""" tests route v2/server_config"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/server_config"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))

		config_dict = json.loads(res.content.decode('utf-8'))

		self.assertTrue('GC_ON_RECOMPRESS' in config_dict.keys())
		self.assertEqual(res.status_code, 200)


@app.route('/api/v2/server_memory_usage', defaults={'nrows':100, 'output_format':'json'}, methods=['GET'])
@app.route('/api/v2/server_memory_usage/<int:nrows>', defaults={'output_format':'json'}, methods=['GET'])
@app.route('/api/v2/server_memory_usage/<int:nrows>/<string:output_format>', methods=['GET'])
def server_memory_usage(nrows, output_format):
	""" returns server memory usage information, as list.
	The server notes memory usage at various key points (pre/post insert; pre/post recompression)
	and these are stored. """
	try:
		result = fn3.server_memory_usage(max_reported = nrows)

	except Exception as e:
		
		capture_exception(e)
		abort(500, e)
	
	# reformat this into a long, human readable format.
	
	resl = pd.melt(pd.DataFrame.from_records(result), id_vars = ['_id','context|time|time_now', 'context|info|message']).dropna()       # drop any na values
	resl.columns = ['_id','event_time','info_message','event_description','value']
	resl = resl[resl.event_description.astype(str).str.startswith('server')]		# only server
	resl['descriptor1']='Server'
	resl['descriptor2']='RAM'
	resl['detail']= [fn3.mhr.convert(x) for x in resl['event_description'].tolist()]
	resl = resl.drop(['event_description'], axis =1)

	if output_format == 'html':
		return(resl.to_html())
	elif output_format == 'json':
		return make_response(resl.to_json(orient='records'))
	else:
		abort(500, "Invalid output_format passed")
		
@app.route('/ui/server_status', defaults={'absdelta':'absolute', 'stats_type':'mstat', 'nrows':1}, methods=['GET'])
@app.route('/ui/server_status/<string:absdelta>/<string:stats_type>/<int:nrows>', methods=['GET'])
def server_storage_status(absdelta, stats_type, nrows):
	""" returns server memory usage information, as list.
	The server notes memory usage at various key points (pre/post insert; pre/post recompression)
	and these are stored."""
	try:
		result = fn3.server_memory_usage(max_reported = nrows)
		df = pd.DataFrame.from_records(result, index='_id')  #, coerce_float=True

		# identify target columns
		valid_starts = ['clusters',
						'guid2meta',
						'guid2neighbour',
						'refcompressedseq',
						'server',
						'mstat',
						'scstat']
		
		if not stats_type in valid_starts:
			abort(404, "Valid stats_type values are {0}".format(valid_starts))
		
		if len(df.columns.values)==0:
			return("No column data found from database query")
		
		target_columns = []
		
		target_string = "{0}".format(stats_type)
		for col in df.columns.values:
			if col.find(target_string)>=0:
				if absdelta=='delta' and col.endswith('|delta'):
					target_columns.append(col)
				elif absdelta=='absolute' and not col.endswith('|delta'):
					target_columns.append(col)
		if nrows<1:
			return("More than one row must be requested.")
		if len(target_columns)==0:
			return("No column data found matching this selection. <p>This may be normal if the server has just started up.<p>We tried to select from {2} rows of data, with {3} columns.  We looked for '{4}'.<p>Valid values for the three variables passed in the URL are as follows: <p> stats_type: {0}. <p> absdelta: ['absolute', 'delta']. <p> nrows must be a positive integer. <p> The columns available for selection from the server's monitoring log are: {1}".format(valid_starts,df.columns.values, len(df.index), len(df.columns.values), target_string))
		if len(df.index)==0:
			return("No row data found matching this selection. <p>This may be normal if the server has just started up.<p> We tried to select from {2} rows of data, with {3} columns.  We looked for '{4}'.<p>Valid values for the three variables passed in the URL are as follows: <p> stats_type: {0}. <p> absdelta: ['absolute', 'delta']. <p> nrows must be a positive integer. <p> The columns available for selection from the server's monitoring log are: {1}".format(valid_starts,df.columns.values, len(df.index), len(df.columns.values), target_string))
		
		# convert x-axis to datetime
		for ix in df.index:
			try:
				df.loc[ix,'context|time|time_now']= dateutil.parser.parse(df.loc[ix,'context|time|time_now'])
			except TypeError:
				app.logger.warning("Attempted date conversion on {0} with _id = {1}, but this failed".format(df.loc[ix,'time|time_now'], ix))
				df.loc[ix,'context|time|time_now']=None
	
		# if values are not completed, then use the previous non-null version
		# see https://stackoverflow.com/questions/14399689/matplotlib-drawing-lines-between-points-ignoring-missing-data
		select_cols = target_columns.copy()
		select_cols.append('context|time|time_now')
		dfp = df[select_cols]
		dfp = dfp.dropna()
		if len(dfp.index)==0:
			return("No non-null row data found matching this selection. <p>This may be normal if the server has just started up.<p> We tried to select from {2} rows of data, with {3} columns.  We looked for '{4}'.<p>Valid values for the three variables passed in the URL are as follows: <p> stats_type: {0}. <p> absdelta: ['absolute', 'delta']. <p> nrows must be a positive integer. <p> The columns available for selection from the server's monitoring log are: {1}".format(valid_starts,df.columns.values, len(df.index), len(df.columns.values), target_string))
		
		# construct a dictionary mapping current column names to human readable versions
		mapper={}
		new_target_columns = []
		for item in target_columns:
			mapper[item] = fn3.mhr.convert(item)
			new_target_columns.append(mapper[item])
		dfp.rename(mapper, inplace=True, axis='columns')

		# plot
		plts = dfp.plot(kind='line', x='context|time|time_now', subplots=True, y=new_target_columns)
		for plt in plts:
			fig = plt.get_figure()
			fig.set_figheight(len(target_columns)*2)
			fig.set_figwidth(8)
			img = io.BytesIO()
			fig.savefig(img)
			matplotlib.pyplot.close('all')		# have to explicitly close, otherwise memory leaks 
			img.seek(0)
			return send_file(img, mimetype='image/png')

	except Exception as e:
		capture_exception(e)
		abort(500, e)
		
	return make_response(tojson(result))

class test_server_memory_usage(unittest.TestCase):
	""" tests route /api/v2/server_memory_usage"""
	def runTest(self):

		relpath = "/api/v2/server_memory_usage"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))

		res = json.loads(res.content.decode('utf-8'))
		self.assertTrue(isinstance(res,list))


@app.route('/api/v2/snpceiling', methods=['GET'])
def snpceiling():
	""" returns largest snp distance stored by the server """
	try:
		result = {"snpceiling":fn3.snpCeiling}
		
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return make_response(tojson(result))

class test_snpceiling(unittest.TestCase):
	""" tests route /api/v2/snpceiling"""
	def runTest(self):
		res = "/api/v2/reset"
		relpath = "/api/v2/snpceiling"
		res = do_POST(relpath, payload={})
		
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		config_dict = json.loads(res.content.decode('utf-8'))
		self.assertTrue('snpceiling' in config_dict.keys())
		self.assertEqual(res.status_code, 200)

@app.route('/api/v2/server_time', methods=['GET'])
def server_time():
	""" returns server time """
	try:
		result = fn3.server_time()

	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return make_response(tojson(result))

@app.route('/api/v2/server_name', methods=['GET'])
def server_name():
	""" returns server name """
	try:
		result = fn3.server_name()

	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return make_response(tojson(result))

class test_server_time(unittest.TestCase):
	""" tests route /api/v2/server_time"""
	def runTest(self):
		relpath = "/api/v2/server_time"
		res = do_GET(relpath)
		print(res)
		self.assertTrue(isjson(content = res.content))
		config_dict = json.loads(res.content.decode('utf-8'))
		self.assertTrue('server_time' in config_dict.keys())
		self.assertEqual(res.status_code, 200)

class test_server_name(unittest.TestCase):
	""" tests route /api/v2/server_name"""
	def runTest(self):
		relpath = "/api/v2/server_name"
		res = do_GET(relpath)
		print(res)
		self.assertTrue(isjson(content = res.content))
		config_dict = json.loads(res.content.decode('utf-8'))
		self.assertTrue('server_name' in config_dict.keys())
		self.assertEqual(res.status_code, 200)
	
@app.route('/api/v2/guids', methods=['GET'])
def get_all_guids(**debug):
	""" returns all guids.  other params, if included, is ignored."""
	try:
		result = list(fn3.get_all_guids())
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return(make_response(tojson(result)))

class test_get_all_guids_1(unittest.TestCase):
	""" tests route /api/v2/guids"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		guidlist = json.loads(str(res.content.decode('utf-8')))
		self.assertTrue(isinstance(guidlist, list))
		self.assertEqual(res.status_code, 200)
		## TODO: insert guids, check it doesn't fail.

@app.route('/api/v2/guids_with_quality_over/<float:cutoff>', methods=['GET'])
@app.route('/api/v2/guids_with_quality_over/<int:cutoff>', methods=['GET'])
def guids_with_quality_over(cutoff, **kwargs):
	""" returns all guids with quality score >= cutoff."""
	try:
		result = fn3.guids_with_quality_over(cutoff)	
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return make_response(tojson(result))

class test_guids_with_quality_over_1(unittest.TestCase):
	""" tests route /api/v2/guids_with_quality_over"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids_with_quality_over/0.7"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		guidlist = json.loads(res.content.decode('utf-8'))
		self.assertTrue(isinstance(guidlist, list))
		self.assertEqual(res.status_code, 200)
		

@app.route('/api/v2/guids_and_examination_times', methods=['GET'])
def guids_and_examination_times(**kwargs):
	""" returns all guids and their examination (addition) time.
	reference, if passed, is ignored."""
	try:	
		result =fn3.get_all_guids_examination_time()	
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return make_response(tojson(result))


class test_get_all_guids_examination_time_1(unittest.TestCase):
	""" tests route /api/v2/guids_and_examination_times"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids_and_examination_times"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		guidlist = json.loads(res.content.decode('utf-8'))
		
		self.assertTrue(isinstance(guidlist, dict))
		self.assertEqual(res.status_code, 200)

		#  test that it actually works
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)

		print("Adding TB reference sequence of {0} bytes".format(len(seq)))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)

		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))

		relpath = "/api/v2/guids_and_examination_times"
		res = do_GET(relpath)
		et= len(json.loads(res.content.decode('utf-8')))

@app.route('/api/v2/guids_beginning_with/<string:startstr>', methods=['GET'])
def get_matching_guids(startstr, max_returned=30):
	""" returns all guids matching startstr.
	A maximum of max_returned matches is returned.
	If > max_returned records match, then an empty list is returned.
	"""
	try:
		result = fn3.gs.search(search_string= startstr, max_returned=max_returned)
		app.logger.debug(result)
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	return(make_response(tojson(result)))



class test_get_matching_guids_1(unittest.TestCase):
	""" tests route /api/v2/guids_beginning_with"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		#  get existing guids
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)

		print("Adding TB reference sequence of {0} bytes".format(len(seq)))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)

		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))

		relpath = "/api/v2/guids_beginning_with/{0}".format(guid_to_insert)
		res = do_GET(relpath)
		self.assertEqual(json.loads(res.content.decode('utf-8')), [guid_to_insert])


@app.route('/api/v2/annotations', methods=['GET'])
def annotations(**kwargs):
	""" returns all guids and associated meta data.
	This query can be slow for very large data sets.
	"""
	try:
		result = fn3.get_all_annotations()
		
	except Exception as e:
		capture_exception(e)
		abort(500, e)
		
	return(tojson(result))

class test_annotations_1(unittest.TestCase):
	""" tests route /api/v2/annotations """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/annotations"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))
		inputDict = json.loads(res.content.decode('utf-8'))
		self.assertTrue(isinstance(inputDict, dict)) 
		guiddf = pd.DataFrame.from_dict(inputDict,orient='index')		#, orient='index'
		self.assertTrue(isinstance(guiddf, pd.DataFrame)) 

@app.route('/api/v2/<string:guid>/exists', methods=['GET'])
def exist_sample(guid, **kwargs):
	""" checks whether a guid exists.
	reference and method are ignored."""
	
	try:
		result = fn3.exist_sample(guid)
		
	except Exception as e:
		capture_exception(e)
		abort(500, e)
		
	return make_response(tojson(result))

class test_exist_sample(unittest.TestCase):
	""" tests route /api/v2/guid/exists """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/exists"
		res = do_GET(relpath)
	   
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), bool)
		self.assertEqual(info, False)

@app.route('/api/v2/<string:guid>/clusters', methods=['GET'])
def clusters_sample(guid):
	""" returns clusters in which a sample resides """
	
	clustering_algorithms = sorted(fn3.clustering.keys())
	retVal=[]
	for clustering_algorithm in clustering_algorithms:

		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)		
		for item in res:
			if item['guid']==guid:
				item['clustering_algorithm']=clustering_algorithm
				retVal.append(item)
	if len(retVal)==0:
		abort(404, "No clustering information for guid {0}".format(guid))
	else:
		return make_response(tojson(retVal))

class test_clusters_sample(unittest.TestCase):
	""" tests route /api/v2/guid/clusters """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		# what happens if there is nothing there
		relpath = "/api/v2/non_existent_guid/clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		
		# add one
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)

		print("Adding TB reference sequence of {0} bytes".format(len(seq)))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))

		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_post = len(json.loads(res.content.decode('utf-8')))
		self.assertEqual(n_pre+1, n_post)
		
		relpath = "/api/v2/{0}/clusters".format(guid_to_insert)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(len(info),3)

class test_clusters_what(unittest.TestCase):
	""" tests implementation of 'what' value, stored in clustering object"""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		# what happens if there is nothing there
		relpath = "/api/v2/non_existent_guid/clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		
		


@app.route('/api/v2/<string:guid>/annotation', methods=['GET'])
def annotations_sample(guid):
	""" returns annotations of one sample """
	
	try:
		result = fn3.PERSIST.guid_annotation(guid)
	
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	
	if len(result.keys())==0:
		abort(404, "guid does not exist {0}".format(guid))
	retVal = result[guid]
	retVal['guid'] = guid
	return make_response(tojson(retVal))

class test_annotation_sample(unittest.TestCase):
	""" tests route /api/v2/guid/annotation """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/annotation"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)

@app.route('/api/v2/insert', methods=['POST'])
def insert():
	""" inserts a guids with sequence"""
	try:
		data_keys = set()
		for key in request.form.keys():
			data_keys.add(key)
		payload = {}
		for key in data_keys:
			payload[key]= request.form[key]
	
		if 'seq' in data_keys and 'guid' in data_keys:
			guid = str(payload['guid'])
			seq  = str(payload['seq'])
			result = fn3.insert(guid, seq)
		else:
			abort(501, 'seq and guid are not present in the POSTed data {0}'.format(data_keys))
		
	except Exception as e:
		capture_exception(e)
		abort(500, e)
		
	return make_response(tojson(result))

@app.route('/api/v2/mirror', methods=['POST'])
def mirror():
	""" receives data, returns the dictionary it was passed. Takes no other action.
	Used for testing that gateways etc don't remove data."""

	retVal = {}
	for key in request.form.keys():
		retVal[key]=request.form[key]
	return make_response(tojson(retVal))

@app.route('/api/v2/clustering', methods=['GET'])
def algorithms():
	"""  returns the available clustering algorithms """
	res = sorted(fn3.clustering.keys())		
	return make_response(tojson({'algorithms':res}))

class test_algorithms(unittest.TestCase):
	"""  tests return of a change_id number """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retDict = json.loads(str(res.text))
		self.assertEqual(retDict, {'algorithms': ['SNV12_ignore', 'SNV12_include','SNV12_include_M']})


@app.route('/api/v2/clustering/<string:clustering_algorithm>/what_tested', methods=['GET'])
def what_tested(clustering_algorithm):
	"""  returns what is tested (N, M, N_or_M) for clustering_algorithm.
		 Useful for producing reports of what clustering algorithms are doing
	"""
	try:
		res = fn3.clustering[clustering_algorithm].uncertain_base_type
	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))
		
	return make_response(tojson({'what_tested': res, 'clustering_algorithm':clustering_algorithm}))

class test_what_tested(unittest.TestCase):
	"""  tests return of what is tested """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_include/what_tested"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retDict = json.loads(str(res.text))
		self.assertEqual(retDict, {'clustering_algorithm': 'SNV12_include', 'what_tested':'N'})
		relpath = "/api/v2/clustering/SNV12_include_M/what_tested"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retDict = json.loads(str(res.text))
		self.assertEqual(retDict, {'clustering_algorithm': 'SNV12_include_M', 'what_tested':'M'})



@app.route('/api/v2/clustering/<string:clustering_algorithm>/change_id', methods=['GET'])
def change_id(clustering_algorithm):
	"""  returns the current change_id number, which is incremented each time a change is made.
		 Useful for recovering changes in clustering after a particular point."""
	try:
		res = fn3.clustering[clustering_algorithm].change_id		
	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))
		
	return make_response(tojson({'change_id': res, 'clustering_algorithm':clustering_algorithm}))

@app.route('/api/v2/clustering/<string:clustering_algorithm>/guids2clusters', methods=['GET'])
def g2c(clustering_algorithm):
	"""  returns a guid -> clusterid dictionary for all guids """
	try:
		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)		
	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))
		
	return make_response(tojson(res))

class test_g2c(unittest.TestCase):
	"""  tests return of guid2clusters data structure """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/guids2clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal, list))

@app.route('/api/v2/clustering/<string:clustering_algorithm>/clusters', methods=['GET'])
@app.route('/api/v2/clustering/<string:clustering_algorithm>/members', methods=['GET'])
@app.route('/api/v2/clustering/<string:clustering_algorithm>/summary', methods=['GET'])
@app.route('/api/v2/clustering/<string:clustering_algorithm>/<int:cluster_id>', methods=['GET'])
def clusters2cnt(clustering_algorithm, cluster_id = None):
	"""  returns a dictionary containing
		 'summary':a clusterid -> count dictionary for all cluster_ids for clustering_algorithm,
		 'members':a list of all guids and clusterids for clustering algorithm
		 
		 * If cluster_id is specified, only returns details for one cluster_id.
		 * If /clusters is requested, returns a dictionary with both 'summary' and 'members' keys.
		 * If /members is requested, returns a dictionary with only 'members' key
		 * If /summary is requested, returns a dictionary with only 'summary' key"""

	try:
		all_res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)

	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))

	# if no cluster_id is specified, then we return all data.
	if cluster_id is None:
		res = all_res
	else:
		
		res = []
		for item in all_res:
			
			if item['cluster_id'] == cluster_id:
				res.append(item)
		if len(res) == 0:
			# no cluster exists of that name
			abort(404, "no cluster {1} exists for algorithm {0}".format(clustering_algorithm, cluster_id))
			
	d= pd.DataFrame.from_records(res)
	
	try:
		df = pd.crosstab(d['cluster_id'],d['is_mixed'])
		df = df.add_prefix('is_mixed_')
		df['cluster_id']=df.index
		summary = json.loads(df.to_json(orient='records'))
		detail  = json.loads(d.to_json(orient='records'))
		#print(request.url, request.url.endswith('summary'), request.url.endswith('members'))
		if cluster_id is not None:
			retVal = {"summary":summary, "members":detail}
		elif request.url.endswith('clusters'):
			retVal = {"summary":summary, "members":detail}			
		elif request.url.endswith('summary'):
			retVal = {"summary":summary}
		elif request.url.endswith('members'):
			retVal = {"members":detail}
		else:
			abort(404, "url not recognised: "+request.url)
	except KeyError:  # no data
		if cluster_id is not None:
			retVal = {"summary":[], "members":[]}
		elif request.url.endswith('clusters'):
			retVal = {"summary":[], "members":[]}			
		elif request.url.endswith('summary'):
			retVal = {"summary":[]}
		elif request.url.endswith('members'):
			retVal = {"members":[]}
		else:
			abort(404, "url not recognised: "+request.url)

	return make_response(tojson(retVal))

class test_clusters2cnt(unittest.TestCase):
	"""  tests return of guid2clusters data structure """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal,dict))
		self.assertEqual(set(retVal.keys()), set(['summary','members']))

		relpath = "/api/v2/clustering/SNV12_ignore/members"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal,dict))
		self.assertEqual(set(retVal.keys()), set(['members']))
		
		relpath = "/api/v2/clustering/SNV12_ignore/summary"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal,dict))
		self.assertEqual(set(retVal.keys()), set(['summary']))
				
class test_cluster2cnt1(unittest.TestCase):
	"""  tests return of guid2clusters data structure """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/0"		# doesn't exist
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)

		# get existing clusterids
		relpath = "/api/v2/clustering/SNV12_ignore/clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal,dict))
		valid_cluster_ids = set()
		for item in retVal['members']:
			valid_cluster_ids.add(item['cluster_id'])
			
		for this_cluster_id in valid_cluster_ids:
			relpath = "/api/v2/clustering/SNV12_ignore/{0}".format(this_cluster_id)		# may exist
			res = do_GET(relpath)
			self.assertEqual(res.status_code, 200)
			
			retVal = json.loads(str(res.text))
			self.assertTrue(isinstance(retVal,dict))
			self.assertTrue(len(retVal['summary'])==1)
			self.assertTrue(len(retVal['members'])>0)
			self.assertEqual(set(retVal.keys()), set(['summary','members']))
			break

@app.route('/api/v2/clustering/<string:clustering_algorithm>/cluster_ids', methods=['GET'])
def g2cl(clustering_algorithm):
	"""  returns a guid -> clusterid dictionary for all guids """
	try:
		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = None)		
	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))
	cluster_ids = set()
	for item in res:
		try:
			cluster_ids.add(item['cluster_id'])
		except KeyError:
			# there's no cluster_id
			pass
	retVal = sorted(list(cluster_ids))
	return make_response(tojson(retVal))

class test_g2cl(unittest.TestCase):
	"""  tests return of a change_id number """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/cluster_ids"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal, list))
@app.route('/api/v2/clustering/<string:clustering_algorithm>/guids2clusters/after_change_id/<int:change_id>', methods=['GET'])
def g2ca(clustering_algorithm, change_id):
	"""  returns a guid -> clusterid dictionary, with changes occurring after change_id, a counter which is incremented each time a change is made.
		 Useful for recovering changes in clustering after a particular point."""
	try:
		res = fn3.clustering[clustering_algorithm].clusters2guidmeta(after_change_id = change_id)		
	except KeyError:
		# no clustering algorithm of this type
		abort(404, "no clustering algorithm {0}".format(clustering_algorithm))
		
	return make_response(tojson(res))

class test_g2ca(unittest.TestCase):
	"""  tests return of a change_id number """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/guids2clusters/after_change_id/1"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal, list))
		
class test_change_id(unittest.TestCase):
	"""  tests return of a change_id number """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/clustering/SNV12_ignore/change_id"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retDict = json.loads(str(res.text))
		self.assertEqual(set(retDict.keys()), set(['change_id','clustering_algorithm']))
		self.assertEqual(retDict['clustering_algorithm'],'SNV12_ignore')

		relpath = "/api/v2/clustering/SNV12_ignore/change_id"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retDict = json.loads(str(res.text))
		self.assertEqual(set(retDict.keys()), set(['change_id','clustering_algorithm']))
		self.assertEqual(retDict['clustering_algorithm'],'SNV12_ignore')

		relpath = "/api/v2/clustering/not_exists/change_id"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)
		

class test_insert_1(unittest.TestCase):
	""" tests route /api/v2/insert """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)

		print("Adding TB reference sequence of {0} bytes".format(len(seq)))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))

		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_post = len(json.loads(res.content.decode('utf-8')))
		self.assertEqual(n_pre+1, n_post)
				

		# check if it exists
		relpath = "/api/v2/{0}/exists".format(guid_to_insert)
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), bool)
		self.assertEqual(res.status_code, 200)
		self.assertEqual(info, True)

class test_insert_10(unittest.TestCase):
	""" tests route /api/v2/insert, with additional samples.
		Also provides a set of very similar samples, testing recompression code."""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
					
		for i in range(1,10):
			guid_to_insert = "guid_{0}".format(n_pre+i)

			seq = originalseq			
			# make i mutations at position 500,000
			offset = 500000
			for j in range(i):
				mutbase = offset+j
				ref = seq[mutbase]
				if not ref == 'T':
					seq[mutbase] = 'T'
				if not ref == 'A':
					seq[mutbase] = 'A'
			seq = ''.join(seq)
						
			print("Adding TB sequence {2} of {0} bytes with {1} mutations relative to ref.".format(len(seq), i, guid_to_insert))
			self.assertEqual(len(seq), 4411532)		# check it's the right sequence
	
			relpath = "/api/v2/insert"
			res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
	
			relpath = "/api/v2/guids"
			res = do_GET(relpath)
			n_post = len(json.loads(res.content.decode('utf-8')))
			self.assertEqual(n_pre+i, n_post)
					
			# check if it exists
			relpath = "/api/v2/{0}/exists".format(guid_to_insert)
			res = do_GET(relpath)
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(type(info), bool)
			self.assertEqual(res.status_code, 200)
			self.assertEqual(info, True)	

class test_insert_10a(unittest.TestCase):
	""" tests route /api/v2/insert, with additional samples.
		Also provides a set of very similar samples, testing mixture addition."""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
					
		for i in range(1,10):
			guid_to_insert = "guid_{0}".format(n_pre+i)

			seq = originalseq			
			# make i mutations at position 500,000
			offset = 500000
			for j in range(i):
				mutbase = offset+j
				ref = seq[mutbase]
				if not ref == 'T':
					seq[mutbase] = 'T'
				if not ref == 'A':
					seq[mutbase] = 'M'
			seq = ''.join(seq)
						
			print("Adding TB sequence {2} of {0} bytes with {1} mutations relative to ref.".format(len(seq), i, guid_to_insert))
			self.assertEqual(len(seq), 4411532)		# check it's the right sequence
	
			relpath = "/api/v2/insert"
			res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
	
			relpath = "/api/v2/guids"
			res = do_GET(relpath)
			n_post = len(json.loads(res.content.decode('utf-8')))
			self.assertEqual(n_pre+i, n_post)
					
			# check if it exists
			relpath = "/api/v2/{0}/exists".format(guid_to_insert)
			res = do_GET(relpath)
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(type(info), bool)
			self.assertEqual(res.status_code, 200)
			self.assertEqual(info, True)	

#@unittest.skip("skipped; to investigate if this ceases timeout")		
class test_insert_60(unittest.TestCase):
	""" tests route /api/v2/insert, with additional samples.
		Also provides a set of very similar samples, testing recompression code."""
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					originalseq = list(str(record.seq))
		guids_inserted = list()			
		for i in range(1,40):
			
			seq = originalseq
			if i % 5 ==0:
				is_mixed = True
				guid_to_insert = "mixed_{0}".format(n_pre+i)
			else:
				is_mixed = False
				guid_to_insert = "nomix_{0}".format(n_pre+i)	
			# make i mutations at position 500,000
			
			offset = 500000
			for j in range(i):
				mutbase = offset+j
				ref = seq[mutbase]
				if is_mixed == False:
					if not ref == 'T':
						seq[mutbase] = 'T'
					if not ref == 'A':
						seq[mutbase] = 'A'
				if is_mixed == True:
						seq[mutbase] = 'N'					
			seq = ''.join(seq)
			guids_inserted.append(guid_to_insert)			
			if is_mixed:
					print("Adding TB sequence {2} of {0} bytes with {1} mutations relative to ref.".format(len(seq), i, guid_to_insert))
			else:
					print("Adding mixed TB sequence {2} of {0} bytes with {1} Ns relative to ref.".format(len(seq), i, guid_to_insert))
				
				
			self.assertEqual(len(seq), 4411532)		# check it's the right sequence
	
			relpath = "/api/v2/insert"
			res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
			self.assertEqual(res.status_code, 200)
		
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(info, 'Guid {0} inserted.'.format(guid_to_insert))
	
			relpath = "/api/v2/guids"
			res = do_GET(relpath)
			n_post = len(json.loads(res.content.decode('utf-8')))
			self.assertEqual(n_pre+i, n_post)
					
			# check if it exists
			relpath = "/api/v2/{0}/exists".format(guid_to_insert)
			res = do_GET(relpath)
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(type(info), bool)
			self.assertEqual(res.status_code, 200)
			self.assertEqual(info, True)	

		# check: is everything there?
		for guid in guids_inserted:
			relpath = "/api/v2/{0}/exists".format(guid)
			res = do_GET(relpath)
			self.assertTrue(isjson(content = res.content))
			info = json.loads(res.content.decode('utf-8'))
			self.assertEqual(type(info), bool)
			self.assertEqual(res.status_code, 200)
			self.assertEqual(info, True)	

		# is everything clustered?
		relpath = "/api/v2/clustering/SNV12_ignore/guids2clusters"
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)
		retVal = json.loads(str(res.text))
		self.assertTrue(isinstance(retVal, list))
		
		# generate MSA
		relpath = "/api/v2/multiple_alignment/guids"
		payload = {'guids':';'.join(guids_inserted),'output_format':'json-records'}
		res = do_POST(relpath, payload=payload)
		self.assertEqual(res.status_code, 200)
		self.assertTrue(isjson(res.content))
		d = json.loads(res.content.decode('utf-8'))
		df = pd.DataFrame.from_records(d)

		#print("running mixed checks:")
		for item in retVal:
			if 'mixed_' in item['guid']:
				#print(item['guid'], item['is_mixed'])
				#self.assertTrue(item['is_mixed'])
				pass
	
class test_mirror(unittest.TestCase):
	""" tests route /api/v2/mirror """
	def runTest(self):
		
		relpath = "/api/v2/mirror"
		payload = {'guid':'1', 'seq':"ACTG"}
		res = do_POST(relpath, payload = payload)
		res_dict = json.loads(res.content.decode('utf-8'))
		self.assertEqual(payload, res_dict)


@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>', methods=['GET'])
@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>/with_quality_cutoff/<float:cutoff>', methods=['GET'])
@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>/with_quality_cutoff/<int:cutoff>', methods=['GET'])
@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>/with_quality_cutoff/<float:cutoff>/in_format/<int:returned_format>', methods=['GET'])
@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>/with_quality_cutoff/<int:cutoff>/in_format/<int:returned_format>', methods=['GET'])
@app.route('/api/v2/<string:guid>/neighbours_within/<int:threshold>/in_format/<int:returned_format>', methods=['GET'])
def neighbours_within(guid, threshold, **kwargs):
	""" get a guid's neighbours, within a threshold """
	# we support optional cutoff and threshold parameters.
	# we also support 'method' and 'reference' parameters but these are ignored.
	# the default for cutoff and format are 0.85 and 1, respectively.
	if not 'cutoff' in kwargs.keys():
		cutoff = CONFIG['MAXN_PROP_DEFAULT']
	else:
		cutoff = kwargs['cutoff']
		
		
	if not 'returned_format' in kwargs.keys():
		returned_format = 1
	else:
		returned_format = kwargs['returned_format']
		
	# validate input
	if not returned_format in set([1,2,3,4]):
		abort(500, "Invalid format requested, must be 1, 2, 3 or 4.")
	if not ( 0 <= cutoff  and cutoff <= 1):
		abort(500, "Invalid cutoff requested, must be between 0 and 1")
		
	try:
		result = fn3.neighbours_within_filter(guid, threshold, cutoff, returned_format)
	except KeyError as e:
		# guid doesn't exist
		abort(404, e)
	except Exception as e:
		capture_exception(e)
		abort(500, e)
	
	return make_response(tojson(result))
	
class test_neighbours_within_1(unittest.TestCase):
	""" tests route /api/v2/guid/neighbours_within/ """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/neighbours_within/12"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		self.assertEqual(res.status_code, 404)

class test_neighbours_within_2(unittest.TestCase):
	""" tests route /api/v2/guid/neighbours_within/ """
	def runTest(self):
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/neighbours_within/12/with_quality_cutoff/0.5"
		res = do_GET(relpath)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		self.assertEqual(res.status_code, 404)

class test_neighbours_within_3(unittest.TestCase):
	""" tests route /api/v2/guid/neighbours_within/ """
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/neighbours_within/12/with_quality_cutoff/0.5/in_format/1"
		res = do_GET(relpath)

		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		self.assertEqual(res.status_code, 404)

class test_neighbours_within_4(unittest.TestCase):
	""" tests route /api/v2/guid/neighbours_within/ """
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/non_existent_guid/neighbours_within/12/with_quality_cutoff/0.5/in_format/2"
		res = do_GET(relpath)

		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		self.assertEqual(res.status_code, 404)

class test_neighbours_within_5(unittest.TestCase):
	""" tests route /api/v2/guid/neighbours_within/ """
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		
		relpath = "/api/v2/non_existent_guid/neighbours_within/12/in_format/2"
		res = do_GET(relpath)
		print(res)
		self.assertTrue(isjson(content = res.content))
		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(type(info), dict)
		self.assertEqual(res.status_code, 404)

class test_neighbours_within_6(unittest.TestCase):
	""" tests all the /api/v2/guid/neighbours_within methods using test data """
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)
					
		# generate variants
		variants = {}
		for i in range(4):
				 guid_to_insert = "guid_insert_{0}".format(n_pre+i+1)
				 vseq=list(seq)
				 vseq[100*i]='A'
				 vseq=''.join(vseq)
				 variants[guid_to_insert] = vseq

		for guid_to_insert in variants.keys():

				print("Adding mutated TB reference sequence called {0}".format(guid_to_insert))        
				relpath = "/api/v2/insert"
				
				res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':variants[guid_to_insert]})
				self.assertTrue(isjson(content = res.content))
				info = json.loads(res.content.decode('utf-8'))
				self.assertTrue('inserted' in info)

				# check if it exists
				relpath = "/api/v2/{0}/exists".format(guid_to_insert)
				res = do_GET(relpath)
				self.assertTrue(isjson(content = res.content))
				info = json.loads(res.content.decode('utf-8'))
				self.assertEqual(type(info), bool)
				self.assertEqual(res.status_code, 200)
				self.assertEqual(info, True)
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_post = len(json.loads(res.content.decode('utf-8')))
		self.assertEqual(n_pre+4, n_post)

		test_guid = min(variants.keys())
		print("Searching for ",test_guid)
		
		search_paths = ["/api/v2/{0}/neighbours_within/1",
						"/api/v2/{0}/neighbours_within/1/with_quality_cutoff/0.5",
						"/api/v2/{0}/neighbours_within/1/with_quality_cutoff/0.5/in_format/1",
						"/api/v2/{0}/neighbours_within/1/with_quality_cutoff/0.5/in_format/2",
						"/api/v2/{0}/neighbours_within/1/with_quality_cutoff/0.5/in_format/3",
						"/api/v2/{0}/neighbours_within/1/with_quality_cutoff/0.5/in_format/4",
						"/api/v2/{0}/neighbours_within/1/in_format/1",
						"/api/v2/{0}/neighbours_within/1/in_format/2",
						"/api/v2/{0}/neighbours_within/1/in_format/3",
						"/api/v2/{0}/neighbours_within/1/in_format/4"
						]
		
		for search_path in search_paths:

				url = search_path.format(test_guid)
				res = do_GET(url)
				self.assertTrue(isjson(content = res.content))
				info = json.loads(res.content.decode('utf-8'))
				self.assertEqual(type(info), list)
				guids_found = set()
				for item in info:
					if isinstance(item,list):
						guids_found.add(item[0])
					elif isinstance(item,dict):
						guids_found.add(item['guid'])
					elif isinstance(item,str):
						guids_found.add(item)
					else:
						self.fail("Unknown class returned {0}".format(type(item)))
				recovered = guids_found.intersection(variants.keys())
				self.assertEqual(len(recovered),3)
				self.assertEqual(res.status_code, 200)

@app.route('/api/v2/<string:guid>/sequence', methods=['GET'])
def sequence(guid):
	""" returns the masked sequence as a string """	
	result = fn3.sequence(guid)
	if result is None:  # no guid exists
		return make_response(tojson('guid {0} does not exist'.format(guid)), 404)
	else:
		return make_response(tojson(result))

class test_sequence_1(unittest.TestCase):
	""" tests route /api/v2/*guid*/sequence"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		n_pre = len(json.loads(str(res.text)))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)

		print("Adding TB reference sequence of {0} bytes".format(len(seq)))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)

		relpath = "/api/v2/{0}/sequence".format(guid_to_insert)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info['guid'], guid_to_insert)
		self.assertEqual(info['invalid'], 0)
		self.assertEqual(info['masked_dna'].count('N'), 557291)

class test_sequence_2(unittest.TestCase):
	""" tests route /api/v2/*guid*/sequence"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/{0}/sequence".format('no_guid_exists')
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 404)

class test_sequence_3(unittest.TestCase):
	""" tests route /api/v2/*guid*/sequence"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		print(res)
		n_pre = len(json.loads(res.content.decode('utf-8')))		# get all the guids

		guid_to_insert = "guid_{0}".format(n_pre+1)

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq = str(record.seq)
		seq = 'N'*4411532
		print("Adding TB reference sequence of {0} bytes with {1} Ns".format(len(seq), seq.count('N')))
		self.assertEqual(len(seq), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert,'seq':seq})
		self.assertEqual(res.status_code, 200)

		relpath = "/api/v2/{0}/sequence".format(guid_to_insert)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		#print(info)
		self.assertEqual(info['guid'], guid_to_insert)
		self.assertEqual(info['invalid'], 1)

class test_sequence_4(unittest.TestCase):
	""" tests route /api/v2/*guid*/sequence"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		#print(res)
		n_pre = len(json.loads(res.content.decode('utf-8')))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq2 = str(record.seq)

		guid_to_insert1 = "guid_{0}".format(n_pre+1)
		guid_to_insert2 = "guid_{0}".format(n_pre+2)


		seq1 = 'N'*4411532
		print("Adding TB reference sequence of {0} bytes with {1} Ns".format(len(seq1), seq1.count('N')))
		self.assertEqual(len(seq1), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert1,'seq':seq1})
		self.assertEqual(res.status_code, 200)

		print("Adding TB reference sequence of {0} bytes with {1} Ns".format(len(seq2), seq2.count('N')))
		self.assertEqual(len(seq2), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert2,'seq':seq2})
		self.assertEqual(res.status_code, 200)
		
		relpath = "/api/v2/{0}/sequence".format(guid_to_insert1)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info['guid'], guid_to_insert1)
		self.assertEqual(info['invalid'], 1)
		relpath = "/api/v2/{0}/sequence".format(guid_to_insert2)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info['guid'], guid_to_insert2)
		self.assertEqual(info['invalid'], 0)

class test_sequence_5(unittest.TestCase):
	""" tests route /api/v2/*guid*/sequence"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "/api/v2/guids"
		res = do_GET(relpath)
		#print(res)
		n_pre = len(json.loads(res.content.decode('utf-8')))		# get all the guids

		inputfile = "../COMPASS_reference/R39/R00000039.fasta"
		with open(inputfile, 'rt') as f:
			for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):               
					seq2 = str(record.seq)

		guid_to_insert1 = "guid_{0}".format(n_pre+1)
		guid_to_insert2 = "guid_{0}".format(n_pre+2)


		seq1 = 'R'*4411532
		print("Adding TB reference sequence of {0} bytes with {1} Rs".format(len(seq1), seq1.count('R')))
		self.assertEqual(len(seq1), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert1,'seq':seq1})
		self.assertEqual(res.status_code, 200)

		print("Adding TB reference sequence of {0} bytes with {1} Ns".format(len(seq2), seq2.count('N')))
		self.assertEqual(len(seq2), 4411532)		# check it's the right sequence

		relpath = "/api/v2/insert"
		res = do_POST(relpath, payload = {'guid':guid_to_insert2,'seq':seq2})
		self.assertEqual(res.status_code, 200)
		
		relpath = "/api/v2/{0}/sequence".format(guid_to_insert1)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info['guid'], guid_to_insert1)
		self.assertEqual(info['invalid'], 1)
		relpath = "/api/v2/{0}/sequence".format(guid_to_insert2)
		res = do_GET(relpath)
		self.assertEqual(res.status_code, 200)

		info = json.loads(res.content.decode('utf-8'))
		self.assertEqual(info['guid'], guid_to_insert2)
		self.assertEqual(info['invalid'], 0)

@app.route('/api/v2/nucleotides_excluded', methods=['GET'])
def nucleotides_excluded():
	""" returns all nucleotides excluded by the server.
	Useful for clients which need to to ensure that server
	and client masking are identical. """
	
	try:
		result = fn3.server_nucleotides_excluded()
		
	except Exception as e:
		capture_exception(e)
		abort(500, e)

	return make_response(tojson(result))

class test_nucleotides_excluded(unittest.TestCase):
	""" tests route /api/v2/nucleotides_excluded"""
	def runTest(self):
		
		relpath = "/api/v2/reset"
		res = do_POST(relpath, payload={})
		
		relpath = "api/v2/nucleotides_excluded"
		res = do_GET(relpath)
		resDict = json.loads(res.text)
		self.assertTrue(isinstance(resDict, dict))
		self.assertEqual(set(resDict.keys()), set(['exclusion_id', 'excluded_nt']))
		self.assertEqual(res.status_code, 200)
 

# startup
if __name__ == '__main__':

	# command line usage.  Pass the location of a config file as a single argument.
	parser = argparse.ArgumentParser(
		formatter_class= argparse.RawTextHelpFormatter,
		description="""Runs findNeighbour3-server, a service for bacterial relatedness monitoring.
									 

Example usage: 
============== 
# show command line options 
python findNeighbour3-server.py --help 	

# run with debug settings; only do this for unit testing.
python findNeighbour3-server.py 	

# run using settings in myConfigFile.json.  Memory will be recompressed after loading. 
python findNeighbour3-server.py ../config/myConfigFile.json		

# run using settings in myConfigFile.json; 
# recompress RAM every 20000 samples loaded 
# (only needed if RAM is in short supply and data close to limit) 
# enabling this option will slow up loading 

python findNeighbour3-server.py ../config/myConfigFile.json	\ 
                        --on_startup_recompress-memory_every 20000 

""")
	parser.add_argument('path_to_config_file', type=str, action='store', nargs='?',
						help='the path to the configuration file', default='')
	parser.add_argument('--on_startup_recompress_memory_every', type=int, nargs=1, action='store', default=[None], 
						help='when loading, recompress server memory every so many samples.')
	args = parser.parse_args()
	
	# an example config file is default_test_config.json

	############################ LOAD CONFIG ######################################
	print("findNeighbour3 server .. reading configuration file.")

	if len(args.path_to_config_file)>0:
			configFile = args.path_to_config_file
	else:
			configFile = os.path.join('..','config','default_test_config.json')
			warnings.warn("No config file name supplied ; using a configuration ('default_test_config.json') suitable only for testing, not for production. ")

	# open the config file
	try:
			with open(configFile,'r') as f:
					 CONFIG=f.read()

	except FileNotFoundError:
			raise FileNotFoundError("Passed a positional parameter, which should be a CONFIG file name; tried to open a config file at {0} but it does not exist ".format(sys.argv[1]))

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

	# determine whether a FNPERSISTENCE_CONNSTRING environment variable is present,
	# if so, the value of this will take precedence over any values in the config file.
	# This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuraton file.
	if os.environ.get("FNPERSISTENCE_CONNSTRING") is not None:
		CONFIG["FNPERSISTENCE_CONNSTRING"] = os.environ.get("FNPERSISTENCE_CONNSTRING")
		print("Set mongodb connection string  from environment variable")
	else:
		print("Using mongodb connection string from configuration file.")

	# determine whether a FN_SENTRY_URLenvironment variable is present,
	# if so, the value of this will take precedence over any values in the config file.
	# This allows 'secret' connstrings involving passwords etc to be specified without the values going into a configuraton file.
	if os.environ.get("FN_SENTRY_URL") is not None:
		CONFIG["SENTRY_URL"] = os.environ.get("FN_SENTRY_URL")
		print("Set Sentry connection string from environment variable")
	else:
		print("Using Sentry connection string from configuration file.")
		
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
	app.logger.setLevel(loglevel)       
	file_handler = logging.FileHandler(CONFIG['LOGFILE'])
	formatter = logging.Formatter( "%(asctime)s | %(pathname)s:%(lineno)d | %(funcName)s | %(levelname)s | %(message)s ")
	file_handler.setFormatter(formatter)
	app.logger.addHandler(file_handler)

	# log a test error on startup
	# app.logger.error("Test error logged on startup, to check logger is working")

	# launch sentry if API key provided
	if 'SENTRY_URL' in CONFIG.keys():
			app.logger.info("Launching communication with Sentry bug-tracking service")
			sentry_sdk.init(CONFIG['SENTRY_URL'], integrations=[FlaskIntegration()])

	########################### prepare to launch server ###############################################################
	# construct the required global variables
	LISTEN_TO = '127.0.0.1'
	if 'LISTEN_TO' in CONFIG.keys():
		LISTEN_TO = CONFIG['LISTEN_TO']

	RESTBASEURL = "http://{0}:{1}".format(CONFIG['IP'], CONFIG['REST_PORT'])

	#########################  CONFIGURE HELPER APPLICATIONS ######################
	## once the flask app is running, errors get logged to app.logger.  However, problems on start up do not.
	## configure mongodb persistence store

	# plotting engine
	matplotlib.use('agg')		#  prevent https://stackoverflow.com/questions/27147300/how-to-clean-images-in-python-django

	if 'SENTRY_URL' in CONFIG.keys():
			app.logger.info("Launching communication with Sentry bug-tracking service")
			sentry_sdk.init(CONFIG['SENTRY_URL'], integrations=[FlaskIntegration()])

	if not 'SERVER_MONITORING_MIN_INTERVAL_MSEC' in CONFIG.keys():
		   CONFIG['SERVER_MONITORING_MIN_INTERVAL_MSEC']=0

	print("Connecting to backend data store")
	try:
			PERSIST=fn3persistence(dbname = CONFIG['SERVERNAME'],
								   connString=CONFIG['FNPERSISTENCE_CONNSTRING'],
								   debug=CONFIG['DEBUGMODE'],
								   server_monitoring_min_interval_msec = CONFIG['SERVER_MONITORING_MIN_INTERVAL_MSEC'])
	except Exception as e:
			app.logger.exception("Error raised on creating persistence object")
			if e.__module__ == "pymongo.errors":
				  app.logger.info("Error raised pertains to pyMongo connectivity")
			raise

	# instantiate server class
	print("Loading sequences into server, please wait ...")
	try:
		fn3 = findNeighbour3(CONFIG, PERSIST, args.on_startup_recompress_memory_every)
	except Exception as e:
			app.logger.exception("Error raised on instantiating findNeighbour3 object")
			raise


	########################  START THE SERVER ###################################
	if CONFIG['DEBUGMODE']>0:
			flask_debug = True
			app.config['PROPAGATE_EXCEPTIONS'] = True
	else:
			flask_debug = False

	app.logger.info("Launching server listening to IP {0}, debug = {1}, port = {2}".format(LISTEN_TO, flask_debug, CONFIG['REST_PORT']))
	app.run(host=LISTEN_TO, debug=flask_debug, port = CONFIG['REST_PORT'])




