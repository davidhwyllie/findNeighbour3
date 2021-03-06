#!/usr/bin/env python3
""" maintains in-ram reference compressed sequence library; allows rapid multithreaded sequence comparisons"""

import unittest
import os
import glob
import sys
import datetime

# storage
import pickle
import hashlib

# multithreading
import multiprocessing
import threading
import time
import multiprocessing
import math

# miscellaneous
import uuid
import json
import psutil
from gzip import GzipFile
import random
import itertools
from collections import Counter

# binomial tests
import numpy as np
from scipy.stats import binom_test
import pandas as pd

# only used for unit testing
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import generic_nucleotide

class ComparingThread (threading.Thread):
    """ wraps the tasks a thread needs to perform when comparing sequences.
    
    Contains a number of methods also present in seqComparer"""
    def __init__(self, threadID, guidList, startIn, endIn, seqProfile, consensi, new_guid, neighbours,
                 compressed_sequence_keys, patch_and_consensus_keys, consensus_keys, snpCeiling):
        threading.Thread.__init__(self)
        self.threadID = threadID                # Thread Id to test which thread compares which samples in the database  
        self.name = "Thread " + str(threadID)   # A name made from threadID
        self.guidList = guidList                # List of all guids to compare against (maybe all in the database)
        self.startIndex = startIn               # Start/end index of the range of sample the thread needs to compare 
        self.endIndex = endIn        
        self.new_guid = new_guid                # The current sequence the thread needs to compare against the database 
        self.neighbours = neighbours            # Dictionary to store the output of comparison 
        self.seqProfile = seqProfile            # Dictionary of 'guid':'sequence'
        self.consensi = consensi                # any consensus sequences used in recompressing seqProfiles

        # parameters which mean the same as those in seqComparer
        self.compressed_sequence_keys = compressed_sequence_keys
        self.patch_and_consensus_keys=patch_and_consensus_keys
        self.consensus_keys = consensus_keys
        self.snpCeiling=snpCeiling
        
    def run(self):  #When the thread starts, it will do the task here
        for i in range(self.startIndex,self.endIndex):#Go thru the index range
            rs = self.countDifferences_byKey((self.new_guid, self.guidList[i]), self.snpCeiling)    #Compare sequence 
            self.neighbours.append(rs)
                
    def apply_patch(self, patch, consensus):
        """ generates a compressed_sequence from a patch and a consensus.
        """
        compressed_sequence = {'invalid':0, 'A':set(),'C':set(),'T':set(),'G':set(),'N':set()}
        for item in ['A','C','T','G','N']:
            # empty sets are not stored in a patch
            if item in patch['add']:
                add_these = patch['add'][item]
            else:
                add_these = set()
            if item in patch['subtract']:
                subtract_these = patch['subtract'][item]
            else:
                subtract_these = set() 
            compressed_sequence[item]=  (consensus[item]|add_these)-subtract_these
        return(compressed_sequence)
    
    def _computeComparator(self, sequence):
        """ generates a reference compressed version of sequence.
        Acceptable inputs are :
        i) a reference compressed version of the sequence
        ii) a reference compressed version relative to a consensus
        """
                   
        if isinstance(sequence, dict):
            try:
                if sequence['invalid']==1:
                    raise ValueError("Cannot uncompress an invalid sequence, as it is not stored. {0}".format(sequence.keys()))
            except KeyError:
                pass
            
            if set(sequence.keys())==self.compressed_sequence_keys:
                return(sequence)
            elif set(sequence.keys())==self.patch_and_consensus_keys:
                return(
                    self.apply_patch(sequence['patch'], self.consensi[sequence['consensus_md5']])
                    ) #decompress relative to a patch
            else:
                raise KeyError("Was passed a dictionary with keys {0} but cannot handle this".format(sequence.keys()))
        else:
            raise TypeError("Cannot use object of class {0} as a sequence".format(type(sequence)))


    def setComparator1(self,sequence):
        """ stores a reference compressed sequence (no patch) in self._seq1. If the sequence is invalid, stores None"""
        try:
            self._seq1=self._computeComparator(sequence)
        except ValueError:
            # it's invalid
            self._seq1 = None
            
    def setComparator2(self,sequence):
        """ stores a reference compressed sequence (no patch) in self._seq2. If the sequence is invalid, stores None. """
        try:
            self._seq2=self._computeComparator(sequence)
        except ValueError:
            # it's invalid
            self._seq2 = None    
        
    def _setStats(self, set1, set2):
        """ compares two sets
        
        returns 
        * the number of elements in set1  4
        * the number of elements in set2  2
        * the number of elements in the union of set1 and set2 5
        * set1 {0,1}
        * set2 {10,11}
        * the union of sorted set1 and set2   {0,1,2,10,11)}
        
        """ 
        retVal=set1 | set2
        return(len(set1), len(set2), len(retVal), set1, set2, retVal)
    def countDifferences(self,cutoff=None):
        """ compares self._seq1 with self._seq2;
        these are set with self.setComparator1 and 2 respectively.
        Returns the number of SNPs between self._seq1 and self._seq2.
        
        Transparently decompresses any sequences stored as deltas relative to a consensus
        scan rate about 25000 per second."""
        #  if cutoff is not specified, we use snpCeiling
        if cutoff is None:
            cutoff = self.snpCeiling
     
        nDiff=0
        if self._seq1 is None or self._seq2 is None:
            return(None)
                 
        if self._seq1['invalid']==1 or self._seq2['invalid']==1:
            return(None)
         
        # compute positions which differ;
        differing_positions = set()
        for nucleotide in ['C','G','A','T']:
       
            # we do not consider differences relative to the reference if the other nucleotide is an N
            nonN_seq1=self._seq1[nucleotide]-self._seq2['N']
            nonN_seq2=self._seq2[nucleotide]-self._seq1['N']
            differing_positions = differing_positions | (nonN_seq1 ^ nonN_seq2)
        
        nDiff = len(differing_positions)
        
        if nDiff>cutoff:
            return(None)
        else:
            return(nDiff)
    
 
    def countDifferences_byKey(self, keyPair, cutoff=None):
        """ compares the in memory refCompressed sequences at
        self.seqProfile[key1] and self.seqProfile[key2]

        Returns the number of SNPs between self._seq1 and self._seq2, and,
        if the pairwise SNP distance is less than cutoff,
        the number of Ns in the two sequences and the union of their positions.
        """

        if not type(keyPair) is tuple:
            raise TypeError("Wanted tuple keyPair, but got keyPair={0} with type {1}".format(keyPair, type(keyPair)))
        if not len(keyPair)==2:
            raise TypeError("Wanted a keyPair with two elements, but got {0}".format(keyPair))
        
        ## test the keys exist
        (key1,key2)=keyPair
        if not key1 in self.seqProfile.keys():
            raise KeyError("Key1={0} does not exist in the in-memory store.".format(key1))
        if not key2 in self.seqProfile.keys():
            raise KeyError("Key1={0} does not exist in the in-memory store.".format(key1))
         
        ## do the computation  
        # if either sequence is considered invalid (e.g. high Ns) then we report no neighbours.
        self.setComparator1(self.seqProfile[key1])
        self.setComparator2(self.seqProfile[key2])
        nDiff=self.countDifferences(cutoff=self.snpCeiling)
        if nDiff is None:
            return((key1, key2, nDiff, None, None, None, None, None, None))
        elif nDiff<=cutoff:
            (n1, n2, nboth, N1pos, N2pos, Nbothpos) = self._setStats(self._seq1['N'],self._seq2['N'])
            return((key1, key2, nDiff, n1,n2,nboth, N1pos, N2pos, Nbothpos))
        else:
            return((key1, key2, nDiff, None, None, None, None, None, None))

class seqComparer():
    def __init__(self,
                    reference,
                    maxNs,
                    snpCeiling,
                    debugMode=False,
                    excludePositions=set(),
                    snpCompressionCeiling = 250,
                    cpuCount = None
                ):

        """ instantiates the sequence comparer, an object which manages in-memory reference compressed sequences.
        
        It does not manage persistence, nor does it automatically load sequences.
        
        reference is a string consisting of the reference sequence.
        This is required because, as a data compression technique,
        only differences from the reference are stored.
               
        excludePositions contains a zero indexed set of bases which should not be considered at all in the sequence comparisons.
        Any bases which are always N should be added to this set.
        Not doing so will substantially degrade the algorithm's performance.
        
        If debugMode==True, the server will only load 500 samples.
        
        If the number of Ns are more than maxNs, no data from the sequence is stored.
        If the number of Ns exceeds nCompressionCutoff, Ns are stored not as single base positions but as ranges.  This markedly reduces memory
        usage if the Ns are in long blocks, but slows the comparison rate from about 5,000 per second to about 150/second.
        
        Results > snpCeiling are not returned or stored.
        
        If snpCompressionCeiling is not None, then will consider samples up to snpCompressionCeiling
        when performing deltas compression relative to close neighbours.
        
        If cpuCount is a positive integer, will multithread up to that number of threads.
        if None, it will multithread multi-sequence comparisons using all available threads.
        if 1, will not use multithreading.
        
        David Wyllie, University of Oxford, June 2018
        
        - to run unit tests, do
        python3 -m unittest seqComparer
        """
        
        # we support three kinds of sequences.
        # sequence in strings;
        # reference based compression relative to reference 'compressed_sequence';
        # reference based compression relative to a consensus 'patch_and_consensus'.
        # we detected the latter two by their keys.
        
        self.compressed_sequence_keys = set(['invalid','A','C','G','T', 'N'])
        self.patch_and_consensus_keys=set(['consensus_md5','patch'])
        self.patch_keys = set(['add','subtract'])
        self.consensus_keys = set(['A','C','G','T', 'N'])
        
        # store snpCeilings.
        self.snpCeiling = snpCeiling
        self.snpCompressionCeiling = snpCompressionCeiling
              
        # sequences with more than maxNs Ns will be considered invalid and their details (apart from their invalidity) will not be stored.
        self.maxNs=maxNs

        # check composition of the reference.
        self.reference=str(reference)           # if passed a Bio.Seq object, coerce to string.
        letters=Counter(self.reference)   
        if len(set(letters.keys())-set(['A','C','G','T']) )>0:
            raise TypeError("Reference sequence supplied contains characters other than ACTG: {0}".format(letters))
                       
        # load the excluded bases
        self.excluded=excludePositions
        
        # define what is included
        self.included=set(range(len(self.reference)))-self.excluded
        
        # initialise pairwise sequences for comparison.
        self._refresh()

        # prepare to load signatures into memory if directed to do so.
        self.seqProfile={}
        self.consensi = {}      # where consensus sequences are stored in ram
        
        # set an appropriate number of threads to use
        nCpus = multiprocessing.cpu_count()
        self.cpuCount = cpuCount
        if self.cpuCount is None:
            self.cpuCount = nCpus
        if self.cpuCount > nCpus:
            self.cpuCount = nCpus
        if self.cpuCount < 1:
            self.cpuCount = 1
 
    def raise_error(self,token):
        """ raises a ZeroDivisionError, with token as the message.
            useful for unit tests of error logging """
        raise ZeroDivisionError(token)
    
    def persist(self, object, guid):
        """ keeps a reference compressed object into RAM.
            Note: the sequences are stored on disc/db relative to the reference.
            Compression relative to each other is carried out post-hoc in ram
            """
        self.seqProfile[guid]=object
    def remove(self, guid):
        """ removes a reference compressed object into RAM.
            If compression relative to other sequences has been carried out post-hoc in ram,
            only the sequence is removed; any consensus linked to it (and potentially to other sequences)
            remain unaltered.
            """
        try:
               del self.seqProfile[guid]
        except KeyError:
               pass 	# we permit attempts to delete things which don't exist

    def load(self, guid):
        """ recovers (loads) a variable containing a reference compressed object into RAM.
            Note: the sequences are stored on disc/db relative to the reference.
            Compression relative to each other is carried out post-hoc in ram
            """
        return self.seqProfile[guid]
      
    def _refresh(self):
        self._seq1=None
        self._seq2=None
        self.seq1md5=None
        self.seq2md5=None

    def mcompare(self, guid, guids=None):
        """ performs multithreaded comparison of guid,
        a stored sample within the server,
        and guids, which are also stored samples.
        if guids is None, all guids are used."""

    
        # if guids are not specified, we do all vs all
        if guids is None:
            guids = set(self.seqProfile.keys())
        
        if not guid in self.seqProfile.keys():
            raise KeyError("Asked to compare {0}  but guid requested has not been stored.  call .persist() on the sample to be added before using mcompare.")
        
        guids = list(set(guids))       
        sampleCount = len(guids)
        neighbours = []
        
        if self.cpuCount == 1:
            # we use a single thread
            for key2 in guids:
                if not guid==key2:
                    (guid1,guid2,dist,n1,n2,nboth, N1pos, N2pos, Nbothpos)=self.countDifferences_byKey(keyPair=(guid,key2),
                                                                                                          cutoff = self.snpCompressionCeiling)            
                    neighbours.append([guid1,guid2,dist,n1,n2,nboth,N1pos, N2pos, Nbothpos])

        else:
            # we multithread
            interval = math.ceil(sampleCount / self.cpuCount) #How many sample each thread should compare with the current sample
            threads = []

            # Create new threads
            for i in range(0,self.cpuCount):
                startIn = i * interval      #Calculate the start index
                endIn = startIn + interval  #End index
                if endIn > sampleCount:     #The last range 
                    endIn = sampleCount    
                #Create a new thread and add it to a list
                threads.append(ComparingThread(i, guids, startIn, endIn, self.seqProfile, self.consensi, guid, neighbours,
                                               self.compressed_sequence_keys, self.patch_and_consensus_keys, self.consensus_keys,
                                               self.snpCeiling))
            
            # Start new Threads 
            for th in threads:
                th.start()
            
            # Wait for them to finish  - don't combine with the above loop  
            for th in threads:
                th.join()
            
        return(neighbours)
    
    def summarise_stored_items(self):
        """ counts how many sequences exist of various types """
        retVal = {}
        retVal['scstat|nSeqs'] = len(self.seqProfile.keys())
        retVal['scstat|nConsensi'] = len(self.consensi.keys())
        retVal['scstat|nInvalid'] = 0
        retVal['scstat|nCompressed'] =0
        retVal['scstat|nRecompressed'] =0
        
        if len(self.seqProfile.keys())==0:
            return(retVal)

        for guid in self.seqProfile.keys():
            if 'invalid' in self.seqProfile[guid]:
                if self.seqProfile[guid]['invalid'] == 1:
                    retVal['scstat|nInvalid'] +=1
            if set(self.seqProfile[guid].keys())==self.patch_and_consensus_keys:
                retVal['scstat|nRecompressed'] +=1
            else:
                retVal['scstat|nCompressed'] +=1
        return(retVal)    
    def iscachedinram(self,guid):
        """ returns true or false depending whether we have a local copy of the refCompressed representation of a sequence (name=guid) in this machine """
        if guid in self.seqProfile.keys():
            return(True)
        else:
            return(False)
    def guidscachedinram(self):
        """ returns all guids with sequence profiles currently in this machine """
        retVal=set()
        for item in self.seqProfile.keys():
            retVal.add(item)
        return(retVal)
    def _guid(self):
        """ returns a new guid, generated de novo """
        return(str(uuid.uuid1()))

    def _delta(self,x):
        """ returns the difference between two numbers in a tuple x """
        return(x[1]-x[0])

    def excluded_hash(self):
        """ returns a string containing the number of nt excluded, and a hash of their positions.
        This is useful for version tracking & storing patterns of masking. """
        l = sorted(list(self.excluded))
        len_l = len(l)
        h = hashlib.md5()
        h.update(json.dumps(l).encode('utf-8'))
        md5_l = h.hexdigest()
        return("Excl {0} nt [{1}]".format(len_l, md5_l))
    
    def uncompress(self, compressed_sequence):
        """ returns a sequence from a compressed_sequence """
        if 'invalid' in compressed_sequence.keys():
            if compressed_sequence['invalid']==1:
                raise ValueError("Cannot uncompress an invalid sequence, because the sequence it is not stored {0}".format(compressed_sequence.keys()))
          
        compressed_sequence = self._computeComparator(compressed_sequence)    # decompress if it is a patch_consensus
        
        seq = list(self.reference)
        
        # mark all positions excluded as N
        for x in self.excluded:
            seq[x]='N'
        for item in ['A','C','T','G','N']:
            for x in compressed_sequence[item]:
                seq[x]=item
        return(''.join(seq))
    
    def compress(self, sequence):
        """ reads a string sequence and extracts position - genome information from it.
        returns a dictionary consisting of zero-indexed positions of non-reference bases.
        
        """
        if not len(sequence)==len(self.reference):
            raise TypeError("sequence must of the same length as reference; seq is {0} and ref is {1}".format(len(sequence),len(self.reference)))
        if len(self.reference)==0:
            raise TypeError("reference cannot be of zero length")
               
        # we consider - characters to be the same as N
        sequence=sequence.replace('-','N')
        
        # we only record differences relative to to refSeq.
        # anything the same as the refSeq is not recorded.
        diffDict={ 'A':set([]),'C':set([]),'T':set([]),'G':set([]),'N':set([])}        

        for i in self.included:     # for the bases we need to compress

            if not sequence[i]==self.reference[i]:
                diffDict[sequence[i]].add(i)
                 
        # convert lists to sets (faster to do this all at once)

        for key in ['A','C','G','T']:
            diffDict[key]=set(diffDict[key])
            
        if len(diffDict['N'])>self.maxNs:
            # we store it, but not with sequence details if is invalid
            diffDict={'invalid':1}
        else:
            diffDict['invalid']=0
            
        return(diffDict)
            
    def _computeComparator(self, sequence):
        """ generates a reference compressed version of sequence.
        Acceptable inputs are :
        i) a string containing sequence
        ii) a reference compressed version of the sequence
        iii) a reference compressed version relative to a consensus
        """
                   
        if isinstance(sequence, str):
            return(self.compress(sequence))
        elif isinstance(sequence, dict):
            try:
                if sequence['invalid']==1:
                    raise ValueError("Cannot uncompress an invalid sequence, as it is not stored. {0}".format(sequence.keys()))
            except KeyError:
                pass
            
            if set(sequence.keys())==self.compressed_sequence_keys:
                return(sequence)
            elif set(sequence.keys())==self.patch_and_consensus_keys:
                return(
                    self.apply_patch(sequence['patch'], self.consensi[sequence['consensus_md5']])
                    ) #decompress relative to a patch
            else:
                raise KeyError("Was passed a dictionary with keys {0} but cannot handle this".format(sequence.keys()))
        else:
            raise TypeError("Cannot use object of class {0} as a sequence".format(type(sequence)))

    
    def setComparator1(self,sequence):
        """ stores a reference compressed sequence (no patch) in self._seq1. If the sequence is invalid, stores None"""
        try:
            self._seq1=self._computeComparator(sequence)
        except ValueError:
            # it's invalid
            self._seq1 = None
            
    def setComparator2(self,sequence):
        """ stores a reference compressed sequence (no patch) in self._seq2. If the sequence is invalid, stores None. """
        try:
            self._seq2=self._computeComparator(sequence)
        except ValueError:
            # it's invalid
            self._seq2 = None    
        
    def _setStats(self, set1, set2):
        """ compares two sets
        
        returns 
        * the number of elements in set1  4
        * the number of elements in set2  2
        * the number of elements in the union of set1 and set2 5
        * set1 {0,1}
        * set2 {10,11}
        * the union of sorted set1 and set2   {0,1,2,10,11)}
        
        """ 
        retVal=set1 | set2
        return(len(set1), len(set2), len(retVal), set1, set2, retVal)

    def countDifferences_byKey(self, keyPair, cutoff=None):
        """ compares the in memory refCompressed sequences at
        self.seqProfile[key1] and self.seqProfile[key2]

        Returns the number of SNPs between self._seq1 and self._seq2, and,
        if the pairwise SNP distance is less than cutoff,
        the number of Ns in the two sequences and the union of their positions.
        """

        if not type(keyPair) is tuple:
            raise TypeError("Wanted tuple keyPair, but got keyPair={0} with type {1}".format(keyPair, type(keyPair)))
        if not len(keyPair)==2:
            raise TypeError("Wanted a keyPair with two elements, but got {0}".format(keyPair))
        
        ## test the keys exist
        (key1,key2)=keyPair
        if not key1 in self.seqProfile.keys():
            raise KeyError("Key1={0} does not exist in the in-memory store.".format(key1))
        if not key2 in self.seqProfile.keys():
            raise KeyError("Key1={0} does not exist in the in-memory store.".format(key1))
         
        # if cutoff is not specified, we use snpCeiling
        if cutoff is None:
            cutoff = self.snpCeiling
            
        ## do the computation  
        # if either sequence is considered invalid (e.g. high Ns) then we report no neighbours.
        self.setComparator1(self.seqProfile[key1])
        self.setComparator2(self.seqProfile[key2])
        nDiff=self.countDifferences(cutoff=cutoff)

        if nDiff is None:
            return((key1, key2, nDiff, None, None, None, None, None, None))
        elif nDiff<=cutoff:
            (n1, n2, nboth, N1pos, N2pos, Nbothpos) = self._setStats(self._seq1['N'],self._seq2['N'])
            return((key1, key2, nDiff, n1,n2,nboth, N1pos, N2pos, Nbothpos))
        else:
            return((key1, key2, nDiff, None, None, None, None, None, None))

    
    def countDifferences(self,cutoff=None):
        """ compares self._seq1 with self._seq2;
        these are set with self.setComparator1 and 2 respectively.
        Returns the number of SNPs between self._seq1 and self._seq2.
        
        Transparently decompresses any sequences stored as deltas relative to a consensus
        scan rate about 25000 per second."""
        #  if cutoff is not specified, we use snpCeiling
        if cutoff is None:
            cutoff = self.snpCeiling
     
        nDiff=0
        if self._seq1 is None or self._seq2 is None:
            return(None)
                 
        if self._seq1['invalid']==1 or self._seq2['invalid']==1:
            return(None)
         
        # compute positions which differ;
        differing_positions = set()
        for nucleotide in ['C','G','A','T']:
       
            # we do not consider differences relative to the reference if the other nucleotide is an N
            nonN_seq1=self._seq1[nucleotide]-self._seq2['N']
            nonN_seq2=self._seq2[nucleotide]-self._seq1['N']
            differing_positions = differing_positions | (nonN_seq1 ^ nonN_seq2)
        
        nDiff = len(differing_positions)
        
        if nDiff>cutoff:
            return(None)
        else:
            return(nDiff)
    
    def consensus(self, compressed_sequences, cutoff_proportion):
        """ from a list of compressed sequences (as generated by compress())
        generate a consensus consisting of the variation present in at least cutoff_proportion of sequences.
        
        returns the consensus object, which is in the same format as that generated by compress()
        """
        
        # for the compressed sequences in the iterable compressed_sequences, compute a frequency distribution of all variants.
        
        # exclude invalid compressed sequences
        valid_compressed_sequences= []
        for compressed_sequence in compressed_sequences:
            compressed_sequence = self._computeComparator(compressed_sequence)
            if compressed_sequence['invalid']==0:
                valid_compressed_sequences.append(compressed_sequence)
                
        # if there are no valid compressed sequences, we return no consensus.
        if len(valid_compressed_sequences)==0:
            return({'A':set(), 'C':set(),'T':set(), 'G':set(), 'N':set()})

        # otherwise we compute the consensus
        counter = dict()
        for item in ['A','C','T','G','N']:
            if not item in counter.keys():
                counter[item]=dict()
            for result in compressed_sequences:
                if item in result.keys():
                    for position in result[item]:
                        if not position in counter[item].keys():
                            counter[item][position]=0
                        counter[item][position]+=1
        
        # next create a diff object reflecting any variants present in at least cutoff_proportion of the time
        cutoff_number = len(compressed_sequences)*cutoff_proportion
        delta = dict()
        for item in ['A','C','T','G','N']:
            if not item in delta.keys():
                delta[item]=set()
                for position in counter[item]:
                    if counter[item][position] >= cutoff_number:
                        delta[item].add(position)
        return(delta)
    
    def generate_patch(self, compressed_sequence, consensus):
        """ generates a 'patch' or difference between a compressed sequence and a consensus.

        anything which is in consensus and compressed_sequence does not need to be in patch;
        anything which is in consensus and not in  compressed_sequence needs are the 'subtract positions';
        anything which is in compressed_sequence and not consensus in  are the 'add positions'
        
        """
        
        add_positions = {'A':set(),'C':set(),'T':set(),'G':set(),'N':set()}
        subtract_positions = {'A':set(),'C':set(),'T':set(),'G':set(),'N':set()}
        for item in ['A','C','T','G','N']:
            add_positions[item] = compressed_sequence[item]-consensus[item]
            subtract_positions[item]= consensus[item]-compressed_sequence[item]
        for item in ['A','C','T','G','N']:
            if len(add_positions[item])==0:
                del add_positions[item]  # don't store empty sets; they cost ~ 120 bytes each
            if len(subtract_positions[item])==0:
                del subtract_positions[item]  # don't store empty sets; they cost ~ 120 bytes each
          
        retVal = {'add':add_positions, 'subtract':subtract_positions}
        return(retVal)
    def apply_patch(self, patch, consensus):
        """ generates a compressed_sequence from a patch and a consensus.
        """
        # sanity check
        if not patch.keys() == self.patch_keys:
            raise TypeError("Patch passed has wrong keys {0}".format(patch.keys))
        if not consensus.keys() == self.consensus_keys:
            raise TypeError("Consensus passed has wrong keys {0}".format(consensus.keys))
        compressed_sequence = {'invalid':0, 'A':set(),'C':set(),'T':set(),'G':set(),'N':set()}
        for item in ['A','C','T','G','N']:
            # empty sets are not stored in a patch
            if item in patch['add']:
                add_these = patch['add'][item]
            else:
                add_these = set()
            if item in patch['subtract']:
                subtract_these = patch['subtract'][item]
            else:
                subtract_these = set()
                
            compressed_sequence[item]=  (consensus[item]|add_these)-subtract_these
        return(compressed_sequence)   
    def compressed_sequence_hash(self, compressed_sequence):
        """ returns a string containing a hash of a compressed object.
        Used for identifying compressed objects, including consensus sequences.
        """
        keys = sorted(compressed_sequence.keys())
        serialised_compressed_sequence = ""
        for key in keys:
            if isinstance(compressed_sequence[key], set):
                l = sorted(list(compressed_sequence[key]))
            else:
                l = compressed_sequence[key]
            serialised_compressed_sequence = serialised_compressed_sequence + key + ":" + str(l) + ';'
        h = hashlib.md5()
        h.update(serialised_compressed_sequence.encode('utf-8'))
        md5 = h.hexdigest()
        return(md5)
    def remove_unused_consensi(self):
        """ identifies and removes any consensi which are not used """
        
        # determine all the consensi which are referred to
        used_consensi_md5 = set()
        for guid in self.seqProfile.keys():
            if 'consensus_md5' in self.seqProfile[guid].keys(): 
                used_consensi_md5.add(self.seqProfile[guid]['consensus_md5'])
        initial_consensi = set(self.consensi.keys())
        for consensus_md5 in initial_consensi:
            if not consensus_md5 in used_consensi_md5:
                del self.consensi[consensus_md5]               
    def compress_relative_to_consensus(self, guid, cutoff_proportion=0.8):
        """ identifies sequences similar to the sequence identified by guid.
        Returns any guids which have been compressed as part of the operation"""
        visited_guids = [guid]
        visited_sequences = [self.seqProfile[guid]]
        for compare_with in self.seqProfile.keys():
            result = self.countDifferences_byKey((guid,compare_with),
                                                 self.snpCompressionCeiling)
            # work outward, finding neighbours of seed_sequence up to self.snpCompressionCeiling
            if result[1] is not guid and result[2] is not None:      # we have a close neighbour
               if result[2]<self.snpCompressionCeiling:
                    # we have found something similar, with which we should compress;
                    visited_sequences.append(self.seqProfile[result[1]])
                    visited_guids.append(result[1])
        
        # compute the consensus for these  and store in consensi
        if len(visited_sequences)>1:    # we can compute a consensus
            consensus = self.consensus(visited_sequences, cutoff_proportion)
            consensus_md5 = self.compressed_sequence_hash(consensus)
            self.consensi[consensus_md5]= consensus
            
            # compress the in-memory instances of these samples
            for guid in visited_guids:
                # decompress the in-memory sequence if it is compressed, and re-compress
                this_seqProfile = self.seqProfile[guid]
                self.seqProfile[guid] = {
                    'patch':self.generate_patch(
                            self._computeComparator(this_seqProfile),
                            consensus),
                    'consensus_md5':consensus_md5
                }
            
            # cleanup; remove any consensi which are not needed
            self.remove_unused_consensi()
        
        # return visited_guids
        return(visited_guids)
    def estimate_expected_N(self, sample_size=30, exclude_guids=set()):
        """ computes the median allN for sample_size guids, randomly selected from all guids except for exclude_guids.
        Used to estimate the expected number of Ns in an alignment """
        
        guids = list(set(self.seqProfile.keys())-set(exclude_guids))
        np.random.shuffle(list(guids))
  
        retVal = None       # cannot compute 
        Ns = []
        for guid in guids:
            try:
                seq = self._computeComparator(self.seqProfile[guid])
                Ns.append(len(seq['N']))
            except ValueError:
                # it is invalid
                pass
            if len(Ns)>=sample_size:
                break
        if len(Ns)>=sample_size:     
            return np.median(Ns)
        else: 
            return None
    def estimate_expected_N_sites(self, sample_size=30, sites = set(), exclude_guids=set()):
        """ computes the median allN for sample_size guids, randomly selected from all guids except for exclude_guids.
        Only reports Ns at sites()
        Used to estimate the expected number of Ns in an alignment """
        
        guids = list(set(self.seqProfile.keys())-set(exclude_guids))
        np.random.shuffle(list(guids))
  
        retVal = None       # cannot compute 
        Ns = []
        for guid in guids:
            try:
                seq = self._computeComparator(self.seqProfile[guid])
                Ns.append(len(seq['N'].intersection(sites)))
            except ValueError:
                # it is invalid
                pass
            if len(Ns)>=sample_size:
                break
        if len(Ns)>=sample_size:     
            return np.median(Ns)
        else: 
            return None
    
    def assess_mixed(self, this_guid, related_guids, max_sample_size=30):
        """ estimates mixture for a single sample, this_guid, by sampling from similar sequences (related_guids)
        in order to determine positions of recent variation.
        
        The strategy used is draw at most max_sample_size unique related_guids, and from them
        analyse all (max_sample_size * (max_sample_size-1))/2 unique pairs.
        For each pair, we determine where they differ, and then
        estimate the proportion of mixed bases in those variant sites.
        
        Pairs of related_guids which do not differ are uninformative and are ignored.

        The output is a pandas dataframe containing mixture estimates for this_guid for each of a series of pairs.

        The p values reported are derived from exact, two-sided binomial tests as implemented in pythons scipy.stats.binom_test().
        
        TEST 1:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by
        i) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the genome.  The estimate_expected_N() function performs this.
        ii) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the relevant  genome.  The estimate_expected_N() function performs this.
          
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
  
        TEST 2:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences
        *at the bases examined in the alignment*.
        This might be relevant if these particular bases are generally hard to call.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base at the relevant sites.  The estimate_expected_N_sites() function performs this.
   
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
      
            
        TEST 3: tests whether the proportion of Ns in the alignment is greater
        than in the bases not in the alignment, for this sequence.
        """
        #print("**STARTING ASSESS_MIXED")
        sample_size = 30        # number of stored sequences to sample in order to estimate the proportion of mixed bases in this population

        # is this_guid mixed?
        comparatorSeq = {}
        try:
            comparatorSeq[this_guid] = self._computeComparator(self.seqProfile[this_guid])
        except ValueError:
            raise ValueError("{0} is invalid".format(this_guid))

        # Estimate expected N as median(observed Ns),
        # which is a valid thing to do if the proportion of mixed samples is low.
        expected_N1 = self.estimate_expected_N(sample_size=sample_size, exclude_guids= related_guids)
        if expected_N1 is None:
            expected_p1 = None
        else:
            expected_p1 = expected_N1 / len(self.reference)
             
        # step 0: find all valid guids in related_guids
        valid_related_guids = []
        invalid_related_guids = []
        comparatorSeq = {}
        for guid in related_guids:
            try:
                comparatorSeq[guid] = self._computeComparator(self.seqProfile[guid])
                valid_related_guids.append(guid)
            except ValueError:
                invalid_related_guids.append(guid)
                
        # randomly sample valid_related_guids
        if len(valid_related_guids)<= max_sample_size:
            sample_valid_related_guids = valid_related_guids
        else:
            np.random.shuffle(valid_related_guids)
            sample_valid_related_guids = valid_related_guids[0:max_sample_size]
 
        # are there any valid related guids?
        if len(sample_valid_related_guids)<2:
            return None     # can't produce any conclusions
        
        # compute pairs
        npairs = 0
        
        for i in range(len(sample_valid_related_guids)):
            for j in range(i):
                #print("** CALLING MSA", i,j, sample_valid_related_guids[i], sample_valid_related_guids[j])
                df = self._msa(valid_guids=[this_guid, sample_valid_related_guids[i], sample_valid_related_guids[j]],
                                            invalid_guids=[],
                                            expected_p1=expected_p1,
                                            output= 'df',
                                            sample_size=30)
                df = df[df.index==this_guid]
                npairs +=1
                df['pairid'] = npairs
                if npairs == 1:
                    retVal = df
                else: 
                    retVal = retVal.append(df, ignore_index=True)
        #print("**MSA: returning {0} rows".format(len(df.index)))
        return(retVal)
    def multi_sequence_alignment(self, guids, output='dict', sample_size=30, expected_p1=None):
        """ computes a multiple sequence alignment containing only sites which vary between guids.
        
        sample_size is the number of samples to randomly sample to estimate the expected number of Ns in
        the population of sequences currently in the server.  From this, the routine computes expected_p1,
        which is expected_expected_N/ the length of sequence.
        if expected_p1 is supplied, then such sampling does not occur.
        
        output can be either
        'dict', in which case the output is presented as dictionaries mapping guid to results; or
        'df' in which case the results is a pandas data frame like the below, where the index consists of the
        guids identifying the sequences, or

            (index)      aligned_seq  allN  alignN   p_value
            AAACGN-1        AAAC     1       0  0.250000
            CCCCGN-2        CCCC     1       0  0.250000
            TTTCGN-3        TTTC     1       0  0.250000
            GGGGGN-4        GGGG     1       0  0.250000
            NNNCGN-5        NNNC     4       3  0.003906
            ACTCGN-6        ACTC     1       0  0.250000
            TCTNGN-7        TCTN     2       1  0.062500
            AAACGN-8        AAAC     1       0  0.250000
            
        'df_dict'.  This is a serialisation of the above, which correctly json serialised.  It can be turned back into a
        pandas DataFrame as follows:
        
        res= sc.multi_sequence_alignment(guid_names[0:8], output='df_dict')     # make the dictionary, see unit test _47
        df = pd.DataFrame.from_dict(res,orient='index')                         # turn it back.
        
        The p values reported are derived from exact, two-sided binomial tests as implemented in pythons scipy.stats.binom_test().

        TEST 1:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by
        i) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the genome.  The estimate_expected_N() function performs this.
        ii) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the relevant  genome.  The estimate_expected_N() function performs this.
          
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
  
        TEST 2:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences
        *at the bases examined in the alignment*.
        This might be relevant if these particular bases are generally hard to call.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base at the relevant sites.  The estimate_expected_N_sites() function performs this.
   
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
      
            
        TEST 3: tests whether the proportion of Ns in the alignment is greater
        than in the bases not in the alignment, for this sequence.
        
        """
        
        # -1 validate input
        if expected_p1 is not None:
            if expected_p1 < 0 or expected_p1 > 1:
                raise ValueError("Expected_p1 must lie between 0 and 1")
        if sample_size is None:
            sample_size = 30

        # step 0: find all valid guids

        valid_guids = []
        invalid_guids = []

        comparatorSeq = {}
        for guid in guids:
            try:
                comparatorSeq[guid] = self._computeComparator(self.seqProfile[guid])
                valid_guids.append(guid)
            except ValueError:
                invalid_guids.append(guid)
        # Estimate expected N as median(observed Ns),
        # which is a valid thing to do if the proportion of mixed samples is low.
        
        if expected_p1 is None:
            expected_N1 = self.estimate_expected_N(sample_size=sample_size, exclude_guids= invalid_guids)
            if expected_N1 is None:
                expected_p1 = None
            else:
                expected_p1 = expected_N1 / len(self.reference)
        else:
            expected_N1 = np.floor(expected_p1 * len(self.reference))
            
        return self._msa(valid_guids, invalid_guids, expected_p1, output, sample_size)
    
    def _msa(self, valid_guids, invalid_guids, expected_p1, output, sample_size):
        """ perform multisequence alignment on the guids in valid_guids, with an
        expected proportion of Ns of expected_p1.
        
        output can be either
        'dict', in which case the output is presented as dictionaries mapping guid to results; or
        'df' in which case the results is a pandas data frame like the below, where the index consists of the
        guids identifying the sequences, or

            (index)      aligned_seq  allN  alignN   p_value
            AAACGN-1        AAAC     1       0  0.250000
            CCCCGN-2        CCCC     1       0  0.250000
            TTTCGN-3        TTTC     1       0  0.250000
            GGGGGN-4        GGGG     1       0  0.250000
            NNNCGN-5        NNNC     4       3  0.003906
            ACTCGN-6        ACTC     1       0  0.250000
            TCTNGN-7        TCTN     2       1  0.062500
            AAACGN-8        AAAC     1       0  0.250000
            
        'df_dict'.  This is a serialisation of the above, which correctly json serialised.  It can be turned back into a
        pandas DataFrame as follows:
        
        res= sc.multi_sequence_alignment(guid_names[0:8], output='df_dict')     # make the dictionary, see unit test _47
        df = pd.DataFrame.from_dict(res,orient='index')                         # turn it back.
        
        The p values reported are derived from exact, two-sided binomial tests as implemented in pythons scipy.stats.binom_test().
        
        TEST 1:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by
        i) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the genome.  The estimate_expected_N() function performs this.
        ii) randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base across the relevant  genome.  The estimate_expected_N() function performs this.
          
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
  
        TEST 2:
        This tests the hypothesis that the number of Ns in the *alignment*
        is GREATER than those expected from the expected_N in the population of whole sequences
        *at the bases examined in the alignment*.
        This might be relevant if these particular bases are generally hard to call.
 
        Does so by comparing the observed number of Ns in the alignment (alignN),
        given the alignment length (4 in the above case) and an expectation of the proportion of bases which will be N.
        The expected number of Ns is estimated by randomly sampling sample_size guids from those stored in the server and
        observing the number of Ns per base at the relevant sites.  The estimate_expected_N_sites() function performs this.
   
        This approach determines the median number of Ns in valid sequences, which (if bad samples with large Ns are rare)
        is a relatively unbiased estimate of the median number of Ns in the good quality samples.
        
        If there  are not enough samples in the server to obtain an estimate, p_value is not computed, being
        reported as None.
      
            
        TEST 3: tests whether the proportion of Ns in the alignment is greater
        than in the bases not in the alignment, for this sequence.

        """
        
        nrps = {}
        comparatorSeq={}
        guid2allNs = {}
        for guid in valid_guids:
            comparatorSeq[guid] = self._computeComparator(self.seqProfile[guid])    
            seq = comparatorSeq[guid]
            guid2allNs[guid] = len(comparatorSeq[guid]['N'])         
            for base in ['A','C','T','G']:
                for position in seq[base]:
                  if not position in nrps.keys():     # if it's non-reference, and we've got no record of this position
                     nrps[position]=set()                    
                  nrps[position].add(base)
                  
        # step 2: for the non-reference positions, check if there's a reference base there.
        for guid in valid_guids:
            seq = comparatorSeq[guid]
            for position in nrps.keys():
                psn_accounted_for  = 0
                for base in ['A','C','T','G','N']:
                    if position in seq[base]:
                        psn_accounted_for = 1
                if psn_accounted_for ==0 :
                    # it is reference
                    nrps[position].add(self.reference[position])
                 
        # step 3: find those which have multiple bases at a position
        variant_positions = set()
        for position in nrps.keys():
            if len(nrps[position])>1:
                variant_positions.add(position)

        # step 4: determine the sequences of all bases.
        ordered_variant_positions = sorted(list(variant_positions))
        guid2seq = {}
        guid2msa_seq={}
        for guid in valid_guids:
            guid2seq[guid]=[]
            seq = comparatorSeq[guid]
            for position in ordered_variant_positions:
                this_base = self.reference[position]
                for base in ['A','C','T','G','N']:
                    if position in seq[base]:
                        this_base = base
                guid2seq[guid].append(this_base)
            guid2msa_seq[guid] = ''.join(guid2seq[guid])
        
        # step 5: determine the expected_p2 at the ordered_variant_positions:
        expected_N2 = self.estimate_expected_N_sites(sample_size=sample_size, exclude_guids= invalid_guids, sites=set(ordered_variant_positions))
        if expected_N2 is None:
            expected_p2 = None
        elif len(ordered_variant_positions) is 0:
            expected_p2 = None
        else:
            expected_p2 = expected_N2 / len(ordered_variant_positions)
        
        # step 6: perform Binomial tests on all samples
        if len(valid_guids)>0:
            guid2pvalue1 = {}
            guid2pvalue2 = {}
            guid2pvalue3 = {}
            guid2alignN = {}
            guid2observed_p = {}
            guid2expected_p1 = {}
            guid2expected_p2 = {}
            guid2expected_p3 = {}
            for guid in valid_guids:
                
                # compute p value 1.  This tests the hypothesis that the number of Ns in the *alignment*
                # is GREATER than those expected from the expected_N in the population of whole sequences.
                guid2alignN[guid]= guid2msa_seq[guid].count('N')
                if expected_p1 is None:     # we don't have an expectation, so we can't assess the first binomial test;
                    p_value1 = None
                    observed_p = None
                elif len(guid2msa_seq[guid])==0:      # we don't have any information to work with
                    p_value1 = None
                    observed_p = None                    
                else:  
                    observed_p = guid2alignN[guid]/len(guid2msa_seq[guid])
                    p_value1 = binom_test(guid2alignN[guid],len(guid2msa_seq[guid]), expected_p1, alternative='greater')
                    
                guid2pvalue1[guid]=p_value1
                guid2observed_p[guid]=observed_p
                guid2expected_p1[guid]=expected_p1
                
                # compute p value 2.  This tests the hypothesis that the number of Ns in the *alignment*
                # is GREATER than those expected from the expected_N in the population of whole sequences
                # at these sites.
                if expected_p2 is None:     # we don't have an expectation, so we can't assess the binomial test;
                    p_value2 = None
                elif len(guid2msa_seq[guid])==0:      # we don't have any information to work with
                    p_value2 = None                
                else:  
                    p_value2 = binom_test(guid2alignN[guid],len(guid2msa_seq[guid]), expected_p2, alternative='greater')                    
                guid2pvalue2[guid]=p_value2
                guid2expected_p2[guid]=expected_p2
                
                                
                 # compute p value 3.  This tests the hypothesis that the number of Ns in the alignment of THIS SEQUENCE
                # is GREATER than the number of Ns not in the alignment  IN THIS SEQUENCE
                # based on sequences not in the alignment

                expected_p3 = (guid2allNs[guid]-guid2alignN[guid])/(len(self.reference)-len(guid2msa_seq[guid]))
                p_value = binom_test(guid2alignN[guid],len(guid2msa_seq[guid]), expected_p3, alternative='greater')
                guid2pvalue3[guid]=p_value
                guid2expected_p3[guid]=expected_p3
                
            # assemble dataframe
            df1 = pd.DataFrame.from_dict(guid2msa_seq, orient='index')
            df1.columns=['aligned_seq']
            df2 = pd.DataFrame.from_dict(guid2allNs, orient='index')
            df2.columns=['allN']
            df3 = pd.DataFrame.from_dict(guid2alignN, orient='index')
            df3.columns=['alignN']
            df4 = pd.DataFrame.from_dict(guid2pvalue1, orient='index')
            df4.columns=['p_value1']
            df5 = pd.DataFrame.from_dict(guid2pvalue2, orient='index')
            df5.columns=['p_value2']
            df6 = pd.DataFrame.from_dict(guid2pvalue2, orient='index')
            df6.columns=['p_value3']
            df7 = pd.DataFrame.from_dict(guid2observed_p, orient='index')
            df7.columns=['observed_proportion']
            df8 = pd.DataFrame.from_dict(guid2expected_p1, orient='index')
            df8.columns=['expected_proportion1']
            df9 = pd.DataFrame.from_dict(guid2expected_p3, orient='index')
            df9.columns=['expected_proportion2']
            df10 = pd.DataFrame.from_dict(guid2expected_p3, orient='index')
            df10.columns=['expected_proportion3']
            
            df = df1.merge(df2, left_index=True, right_index=True)
            df = df.merge(df3, left_index=True, right_index=True)
            df = df.merge(df4, left_index=True, right_index=True)
            df = df.merge(df5, left_index=True, right_index=True)
            df = df.merge(df6, left_index=True, right_index=True)
            df = df.merge(df7, left_index=True, right_index=True)
            df = df.merge(df8, left_index=True, right_index=True)
            df = df.merge(df9, left_index=True, right_index=True)         
            df = df.merge(df10, left_index=True, right_index=True)
                  
            retDict = {'variant_positions':ordered_variant_positions,
                    'invalid_guids': invalid_guids,
                    'guid2sequence':guid2seq,
                    'guid2allN':guid2allNs,
                    'guid2msa_seq':guid2msa_seq,
                    'guid2observed_proportion':guid2observed_p,
                    'guid2expected_p1':guid2expected_p1,
                    'guid2expected_p2':guid2expected_p2,
                    'guid2expected_p3':guid2expected_p3,
                    'guid2pvalue1':guid2pvalue1,
                    'guid2pvalue2':guid2pvalue2,
                    'guid2pvalue3':guid2pvalue3,
                    'guid2alignN':guid2alignN}
        
        else:
            return None
                
        if output=='dict':    
            return(retDict)
        elif output=='df':
            return(df)
        elif output=='df_dict':
            return(df.to_dict(orient='index'))
        else:
            raise ValueError("Don't know how to format {0}.  Valid options are {'df','dict'}".format(output))

class test_seqComparer_51(unittest.TestCase):
    """ tests assess_mixed when there is no difference between samples analysed """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
    
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','AAACGN','AAACGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res = sc.assess_mixed(this_guid='AAACGN-1', related_guids=['AAACGN-2','AAACGN-3'],max_sample_size=5)
        self.assertEqual(len(res.index), 1)


class test_seqComparer_50b(unittest.TestCase):
    """ tests assess_mixed """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
    
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res = sc.assess_mixed(this_guid='AAACGN-1', related_guids=[],max_sample_size=5)
        self.assertEqual(res, None)


class test_seqComparer_50a(unittest.TestCase):
    """ tests assess_mixed """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
    
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res = sc.assess_mixed(this_guid='AAACGN-1', related_guids=['CCCCGN-2','TTTCGN-3','GGGGGN-4','NNNCGN-5','ACTCGN-6', 'TCTNGN-7'],max_sample_size=5)
        self.assertEqual(res.columns.tolist(),['aligned_seq', 'allN', 'alignN', 'p_value1', 'p_value2', 'p_value3', 'observed_proportion',
                                               'expected_proportion1', 'expected_proportion2', 'expected_proportion3', 'pairid'])
        for ix in res.index:
            self.assertEqual(res.loc[ix,'p_value1'],1)
            self.assertEqual(res.loc[ix,'p_value2'],1)
            self.assertEqual(res.loc[ix,'p_value3'],1)
        self.assertEqual(len(res.index), 10)

class test_seqComparer_49(unittest.TestCase):
    """ tests reporting on stored contents """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res = sc.summarise_stored_items()
        self.assertTrue(isinstance(res, dict))
        self.assertEqual(set(res.keys()), set(['scstat|nSeqs', 'scstat|nConsensi', 'scstat|nInvalid', 'scstat|nCompressed', 'scstat|nRecompressed']))
class test_seqComparer_48(unittest.TestCase):
    """ tests computations of p values from exact bionomial test """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

class test_seqComparer_47c(unittest.TestCase):
    """ tests generation of a multisequence alignment with
        testing for the proportion of Ns.
        Tests situation with externally supplied _p1"""
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']

        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        # but with expected_N supplied;
        df= sc.multi_sequence_alignment(guid_names[0:8], output='df', expected_p1=0.995)      
        # there's variation at positions 0,1,2,3
        self.assertTrue(isinstance(df, pd.DataFrame))

        self.assertEqual(set(df.columns.values),set(['aligned_seq','allN','alignN','p_value1','p_value2','p_value3', 'observed_proportion','expected_proportion1','expected_proportion2','expected_proportion3']))
        self.assertEqual(len(df.index),7)
        res= sc.multi_sequence_alignment(guid_names[0:8], output='df_dict', expected_p1=0.995)
        df = pd.DataFrame.from_dict(res,orient='index')

        self.assertEqual(set(df.columns.values),set(['aligned_seq', 'allN', 'alignN', 'p_value1', 'p_value2', 'p_value3', 'observed_proportion',
                                               'expected_proportion1', 'expected_proportion2', 'expected_proportion3']))
    
        self.assertEqual(set(df.index.tolist()), set(['AAACGN-1','CCCCGN-2','TTTCGN-3','GGGGGN-4','ACTCGN-6', 'TCTNGN-7','AAACGN-8']))
        self.assertTrue(df.loc['AAACGN-1','expected_proportion1'] is not None)        # check it computed a value
        self.assertEqual(df.loc['AAACGN-1','expected_proportion1'], 0.995)        # check is used the value passed


class test_seqComparer_47b(unittest.TestCase):
    """ tests generation of a multisequence alignment with
        testing for the proportion of Ns.
        Tests all three outputs."""
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']

        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res= sc.multi_sequence_alignment(guid_names[0:8], output='dict')

        # there's variation at positions 0,1,2,3
        self.assertEqual(res['variant_positions'],[0,1,2,3])
        df= sc.multi_sequence_alignment(guid_names[0:8], output='df')
        
        # there's variation at positions 0,1,2,3
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertEqual(set(df.columns.values),set(['aligned_seq','allN','alignN','p_value1','p_value2','p_value3', 'observed_proportion','expected_proportion1','expected_proportion2','expected_proportion3']))
        self.assertEqual(len(df.index),7)
        res= sc.multi_sequence_alignment(guid_names[0:8], output='df_dict')
        df = pd.DataFrame.from_dict(res,orient='index')
        self.assertTrue(df.loc['AAACGN-1','expected_proportion1'] is not None)        # check it computed a value
        self.assertEqual(set(df.index.tolist()), set(['AAACGN-1','CCCCGN-2','TTTCGN-3','GGGGGN-4','ACTCGN-6', 'TCTNGN-7','AAACGN-8']))

class test_seqComparer_47a(unittest.TestCase):
    """ tests generation of a multisequence alignment with
        testing for the proportion of Ns.
        Tests all three outputs."""
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        # need > 30 sequences
        originals = ['AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN',
                     'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN','AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN']
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res= sc.multi_sequence_alignment(guid_names[0:8], output='dict')
        # there's variation at positions 0,1,2,3
        self.assertEqual(res['variant_positions'],[0,1,2,3])

        df= sc.multi_sequence_alignment(guid_names[0:8], output='df')
        # there's variation at positions 0,1,2,3
        self.assertTrue(isinstance(df, pd.DataFrame))
        self.assertEqual(set(df.columns.values),set(['aligned_seq','allN','alignN','p_value1','p_value2','p_value3', 'observed_proportion','expected_proportion1','expected_proportion2','expected_proportion3']))
        self.assertEqual(len(df.index),8)
        res= sc.multi_sequence_alignment(guid_names[0:8], output='df_dict')
        df = pd.DataFrame.from_dict(res,orient='index')
    
        self.assertEqual(set(df.index.tolist()), set(guid_names[0:8]))
 
class test_seqComparer_46a(unittest.TestCase):
    """ tests estimate_expected_N, a function estimating the number of Ns in sequences
        by sampling """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        n=0
        originals = [ 'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN' ]
        guids = []
        for original in originals:
            n+=1
            c = sc.compress(original)
            guid = "{0}-{1}".format(original,n )
            guids.append(guid)
            sc.persist(c, guid=guid)
          
        res = sc.estimate_expected_N()      # defaults to sample size 30
        self.assertEqual(res, None)
        
        # analyse the last two
        res = sc.estimate_expected_N(sample_size=2, exclude_guids = guids[0:5])      
        self.assertEqual(res, 1.5)

        # analyse the first two
        res = sc.estimate_expected_N(sample_size=2, exclude_guids = guids[2:7])      
        self.assertEqual(res, 1)
class test_seqComparer_46b(unittest.TestCase):
    """ tests estimate_expected_N, a function estimating the number of Ns in sequences
        by sampling """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        n=0
        originals = [ 'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTGGN' ]
        guids = []
        for original in originals:
            n+=1
            c = sc.compress(original)
            guid = "{0}-{1}".format(original,n )
            guids.append(guid)
            sc.persist(c, guid=guid)
          
        res = sc.estimate_expected_N()      # defaults to sample size 30
        self.assertEqual(res, None)
        
        # analyse them all
        res = sc.estimate_expected_N(sample_size=7, exclude_guids = [])      
        self.assertEqual(res, None)

        # analyse them all
        res = sc.estimate_expected_N(sample_size=6, exclude_guids = [])      
        self.assertEqual(res, 1)
class test_seqComparer_46c(unittest.TestCase):
    """ tests estimate_expected_N_sites, a function estimating the number of Ns in sequences
        by sampling """
    def runTest(self):
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        n=0
        originals = [ 'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN' ]
        guids = []
        for original in originals:
            n+=1
            c = sc.compress(original)
            guid = "{0}-{1}".format(original,n )
            guids.append(guid)
            sc.persist(c, guid=guid)
                 
        # analyse nothing
        res = sc.estimate_expected_N_sites(sample_size=2, sites=set([]), exclude_guids = guids[0:5])      
        self.assertEqual(res, 0)

        # analyse the last two
        res = sc.estimate_expected_N_sites(sample_size=2, sites=set([0,1,2,3,4,5]), exclude_guids = guids[0:5])      
        self.assertEqual(res, 1.5)

        # analyse the first two
        res = sc.estimate_expected_N_sites(sample_size=2, sites=set([0,1,2,3,4,5]), exclude_guids = guids[2:7])      
        self.assertEqual(res, 1)       
class test_seqComparer_45a(unittest.TestCase):
    """ tests the generation of multiple alignments of variant sites."""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = [ 'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTNGN' ]
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res= sc.multi_sequence_alignment(guid_names)
        # there's variation at positions 0,1,2,3
        df = pd.DataFrame.from_dict(res['guid2sequence'], orient='index')
        df.columns=res['variant_positions']
        self.assertEqual(len(df.index), 7)
        self.assertEqual(res['variant_positions'],[0,1,2,3])

        
class test_seqComparer_45b(unittest.TestCase):
    """ tests the generation of multiple alignments of variant sites."""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = [ 'AAACGN','CCCCGN','TTTCGN','GGGGGN','NNNCGN','ACTCGN', 'TCTGGN' ]
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res= sc.multi_sequence_alignment(guid_names)
        # there's variation at positions 0,1,2,3
        df = pd.DataFrame.from_dict(res['guid2sequence'], orient='index')
        df.columns=res['variant_positions']
        self.assertEqual(len(df.index), 6)
        self.assertEqual(res['variant_positions'],[0,1,2,3])

class test_seqComparer_45c(unittest.TestCase):
    """ tests the generation of multiple alignments of variant sites."""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGGGG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = ['NNNCGN' ]
        guid_names = []
        n=0
        for original in originals:
            n+=1
            c = sc.compress(original)
            this_guid = "{0}-{1}".format(original,n )
            sc.persist(c, guid=this_guid)
            guid_names.append(this_guid)

        res= sc.multi_sequence_alignment(guid_names)
        self.assertTrue(res is None)
   
class test_seqComparer_1(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        self.assertEqual(sc.reference,refSeq)     
class test_seqComparer_2(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        with self.assertRaises(TypeError):
            retVal=sc.compress(sequence='AC')
class test_seqComparer_3(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq )
        retVal=sc.compress(sequence='ACTG')
        self.assertEqual(retVal,{'G': set([]), 'A': set([]), 'C': set([]), 'T': set([]), 'N': set([]), 'invalid':0})
class test_seqComparer_4(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)

        retVal=sc.compress(sequence='ACTN')
        self.assertEqual(retVal,{'G': set([]), 'A': set([]), 'C': set([]), 'T': set([]), 'N': set([3]), 'invalid':0})
class test_seqComparer_5(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        retVal=sc.compress(sequence='ACT-')
        self.assertEqual(retVal,{'G': set([]), 'A': set([]), 'C': set([]), 'T': set([]), 'N': set([3]), 'invalid':0})         
class test_seqComparer_6(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)

        retVal=sc.compress(sequence='TCT-')
        self.assertEqual(retVal,{'G': set([]), 'A': set([]), 'C': set([]), 'T': set([0]), 'N': set([3]), 'invalid':0})
class test_seqComparer_7(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        retVal=sc.compress(sequence='ATT-')
        self.assertEqual(retVal,{ 'G': set([]), 'A': set([]), 'C': set([]), 'T': set([1]), 'N': set([3]), 'invalid':0})

class test_seqComparer_6b(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        originals = [ 'AAAA','CCCC','TTTT','GGGG','NNNN','ACTG','ACTC', 'TCTN']
        for original in originals:

            compressed_sequence=sc.compress(sequence=original)
          
            roundtrip = sc.uncompress(compressed_sequence)
            self.assertEqual(original, roundtrip)

class test_seqComparer_6c(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        originals = [ 'NNNN']
        for original in originals:

            compressed_sequence=sc.compress(sequence=original)
            roundtrip = sc.uncompress(compressed_sequence)
            self.assertEqual(original, roundtrip)

class test_seqComparer_6d(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'

        sc=seqComparer( maxNs = 3, snpCeiling = 20,reference=refSeq)
        originals = [ 'NNNN']
        for original in originals:

            compressed_sequence=sc.compress(sequence=original)
            with self.assertRaises(ValueError):
                roundtrip = sc.uncompress(compressed_sequence)
          
class test_seqComparer_8(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer(maxNs = 1e8, snpCeiling = 20,reference=refSeq)

        sc.setComparator1(sequence='ACTG')
        sc.setComparator2(sequence='ACTG')

class test_seqComparer_9(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator1(sequence='ACTG')
        sc.setComparator2(sequence='ACTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_10(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator1(sequence='TTTG')
        sc.setComparator2(sequence='ACTG')
        self.assertEqual(sc.countDifferences(),2)
class test_seqComparer_11(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator1(sequence='TTTG')
        sc.setComparator2(sequence='NNTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_12(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)

        sc.setComparator2(sequence='TTTG')
        sc.setComparator1(sequence='NNTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_13(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator2(sequence='TTTG')
        sc.setComparator1(sequence='--TG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_14(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator2(sequence='TTAA')
        sc.setComparator1(sequence='--AG')
        self.assertEqual(sc.countDifferences(),1)
class test_seqComparer_15(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        sc.setComparator1(sequence='TTAA')
        sc.setComparator2(sequence='--AG')
        self.assertEqual(sc.countDifferences(),1)
        
class test_seqComparer_16(unittest.TestCase):
    """ tests the comparison of two sequences where both differ from the reference. """
    def runTest(self):   
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        
        sc._seq1 = sc.compress('AAAA')
        sc._seq2 = sc.compress('CCCC')
        self.assertEqual(sc.countDifferences(),4)

class test_seqComparer_17(unittest.TestCase):
    """ tests the comparison of two sequences where one is invalid """
    def runTest(self):   
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 3,
                       reference=refSeq,
                       snpCeiling =10)
        
        sc._seq1 = sc.compress('AAAA')
        sc._seq2 = sc.compress('NNNN')
        self.assertEqual(sc.countDifferences(),None)
class test_seqComparer_saveload3(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        compressedObj =sc.compress(sequence='ACTT')
        sc.persist(compressedObj, 'one' )     
        retVal=sc.load(guid='one' )
        self.assertEqual(compressedObj,retVal)        
class test_seqComparer_save_remove(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        compressedObj =sc.compress(sequence='ACTT')
        sc.persist(compressedObj, 'one' )     
        retVal=sc.iscachedinram(guid='one' )
        self.assertEqual(True,retVal)        
        sc.remove('one')
        retVal=sc.iscachedinram(guid='one' )
        self.assertEqual(False,retVal)  
class test_seqComparer_24(unittest.TestCase):
    """ tests N compression """
    def runTest(self):
        
        refSeq=                     'ACTGTTAATTTTTTTTTGGGGGGGGGGGGAA'
        sc=seqComparer(maxNs = 1e8, snpCeiling = 20,reference=refSeq)

        retVal=sc.compress(sequence='ACTGTTAANNNNNNNNTGGGGGGGGGGGGAA')
        self.assertEqual(retVal,{ 'G': set([]), 'A': set([]), 'C': set([]), 'T': set([]), 'N': set([8,9,10,11,12,13,14,15]), 'invalid':0})
        retVal=sc.compress(sequence='NNTGTTAANNNNNNNNTGGGGGGGGGGGGAA')
        self.assertEqual(retVal,{ 'G': set([]), 'A': set([]), 'C': set([]), 'T': set([]), 'N': set([0,1,8,9,10,11,12,13,14,15]), 'invalid':0})
       
class test_seqComparer_29(unittest.TestCase):
    """ tests _setStats """
    def runTest(self):
        
        refSeq=                             'ACTGTTAATTTTTTTTTGGGGGGGGGGGGAA'
        sc=seqComparer(maxNs = 1e8, snpCeiling = 20,reference=refSeq)
        compressedObj1=sc.compress(sequence='GGGGTTAANNNNNNNNNGGGGGAAAAGGGAA')
        compressedObj2=sc.compress(sequence='ACTGTTAATTTTTTTTTNNNNNNNNNNNNNN')
        (n1,n2,nall,rv1,rv2,retVal) =sc._setStats(compressedObj1['N'],compressedObj2['N'])
        self.assertEqual(retVal, set([8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30]))
 
        compressedObj1=sc.compress(sequence='GGGGTTAANNNNNNNNTGGGGGAAAAGGGAA')
        compressedObj2=sc.compress(sequence='ACTGTTAATTTTTTTTTNNNNNNNNNNNNNN')
        (n1,n2,nall,rv1,rv2,retVal)=sc._setStats(compressedObj1['N'],compressedObj2['N'])
        self.assertEqual(retVal,set([8,9,10,11,12,13,14,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30]))
        
        compressedObj1=sc.compress(sequence='NNNGTTAANNNNNNNNTGGGGGAAAAGGGAA')
        compressedObj2=sc.compress(sequence='ACTGTTAATTTTTTTTTNNNNNNNNNNNNNN')
        (n1,n2,nall,rv1,rv2,retVal)=sc._setStats(compressedObj1['N'],compressedObj2['N'])
        self.assertEqual(retVal,set([0,1,2,8,9,10,11,12,13,14,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30]))


        compressedObj1=sc.compress(sequence='NNNGTTAANNNNNNNNTGGGGGAAAAGGGAA')
        compressedObj2=sc.compress(sequence='ACTNNNNNTTTTTTTTTNNNNNNNNNNNNNN')
        (n1,n2,nall,rv1,rv2,retVal)=sc._setStats(compressedObj1['N'],compressedObj2['N'])
        self.assertEqual(retVal,set([0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30]))
 
        
class test_seqComparer_30(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling= 1)
        sc.setComparator1(sequence='ACTG')
        sc.setComparator2(sequence='ACTG')
class test_seqComparer_31(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1 )
        sc.setComparator1(sequence='ACTG')
        sc.setComparator2(sequence='ACTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_32(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator1(sequence='TTTG')
        sc.setComparator2(sequence='ACTG')
        self.assertEqual(sc.countDifferences(),None)
class test_seqComparer_33(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator1(sequence='TTTG')
        sc.setComparator2(sequence='NNTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_34(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator2(sequence='TTTG')
        sc.setComparator1(sequence='NNTG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_35(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 2, reference=refSeq, snpCeiling =1)
        sc.setComparator2(sequence='TTTG')
        sc.setComparator1(sequence='NNNG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_13(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator2(sequence='TTTG')
        sc.setComparator1(sequence='--TG')
        self.assertEqual(sc.countDifferences(),0)
class test_seqComparer_35(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator2(sequence='TTAA')
        sc.setComparator1(sequence='--AG')
        self.assertEqual(sc.countDifferences(),1)
class test_seqComparer_36(unittest.TestCase):
    def runTest(self):
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        sc.setComparator1(sequence='TTAA')
        sc.setComparator2(sequence='--AG')
        self.assertEqual(sc.countDifferences(),1)
class test_seqComparer_37(unittest.TestCase):
    """ tests the loading of an exclusion file """
    def runTest(self):
        
        # default exclusion file
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        self.assertEqual( sc.excluded_hash(), 'Excl 0 nt [d751713988987e9331980363e24189ce]')

class test_seqComparer_38(unittest.TestCase):
    """ tests the loading of an exclusion file """
    def runTest(self):
        
        # no exclusion file
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =1)
        self.assertEqual( sc.excluded_hash(), 'Excl 0 nt [d751713988987e9331980363e24189ce]')


class test_seqComparer_39a(unittest.TestCase):
    """ tests the computation of a consensus sequence """
    def runTest(self):
        
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =10)
        compressed_sequences = []
        compressed_sequences.append(sc.compress(sequence='TTAA'))
        compressed_sequences.append(sc.compress(sequence='TTTA'))
        compressed_sequences.append(sc.compress(sequence='TTGA'))
        compressed_sequences.append(sc.compress(sequence='TTAA'))

        cutoff_proportion = 0.5
        consensus = sc.consensus(compressed_sequences, cutoff_proportion)

        expected_consensus = { 'T': {0, 1}, 'N': set(), 'A': {2, 3}, 'C': set(), 'G': set()}       
        self.assertEqual(consensus, expected_consensus)


class test_seqComparer_39b(unittest.TestCase):
    """ tests the computation of a consensus sequence """
    def runTest(self):
        
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =10)
        compressed_sequences = []
        
        cutoff_proportion = 0.5
        delta = sc.consensus(compressed_sequences, cutoff_proportion)

        expected_delta = {'A':set(), 'C':set(),'T':set(), 'G':set(), 'N':set()} 
        
        self.assertEqual(delta, expected_delta)
          
  
class test_seqComparer_40(unittest.TestCase):
    """ tests the computation of a hash of a compressed object """
    def runTest(self):

        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =10)
        compressed_sequence = sc.compress(sequence='TTAA')

        res = sc.compressed_sequence_hash(compressed_sequence)
        self.assertEqual(res, "23b867b142bad108b848b87ad4b79633")
        
        
class test_seqComparer_41(unittest.TestCase):
    """ tests the computation of a difference relative to a reference + delta """
    def runTest(self):
        
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8, reference=refSeq, snpCeiling =10)
        compressed_sequences = []
        compressed_sequences.append(sc.compress(sequence='TTAA'))
        compressed_sequences.append(sc.compress(sequence='TTTA'))
        compressed_sequences.append(sc.compress(sequence='TTGA'))
        compressed_sequences.append(sc.compress(sequence='TTAA'))

        cutoff_proportion = 0.5
        consensus = sc.consensus(compressed_sequences, cutoff_proportion)

        originals = [ 'AAAA','CCCC','TTTT','GGGG','NNNN','ACTG','ACTC', 'TCTN' ]
        for original in originals:

            compressed_sequence = sc.compress(sequence=original)
            patch = sc.generate_patch(compressed_sequence, consensus)
            roundtrip_compressed_sequence = sc.apply_patch(patch, consensus)
            self.assertEqual(compressed_sequence, roundtrip_compressed_sequence)
            roundtrip = sc.uncompress(roundtrip_compressed_sequence)
            self.assertEqual(roundtrip, original)

class test_seqComparer_42(unittest.TestCase):
    """ tests the compression relative to a consensus """
    def runTest(self):
        
        # generate compressed sequences
        refSeq='ACTG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = [ 'AAAC','CCCC','TTTC','GGGC','NNNC','ACTC','ACTC', 'TCTN' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        sc.compress_relative_to_consensus(guid = 'AAAC', cutoff_proportion = 0.5)
        
        for original in originals:
            self.assertEqual(original, sc.uncompress(sc.seqProfile[original]))

class test_seqComparer_43(unittest.TestCase):
    """ tests the compression relative to a consensus with a consensus present"""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGGGGGGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = [ 'AAACACTGACTG','CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG','NNNCACTGACTG','ACTCACTGACTG','ACTCACTGACTG', 'TCTNACTGACTG' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        sc.compress_relative_to_consensus(guid = 'AAACACTGACTG', cutoff_proportion = 0.8)
        
        for original in originals:
            self.assertEqual(original, sc.uncompress(sc.seqProfile[original]))

class test_seqComparer_44(unittest.TestCase):
    """ tests the compression relative to a consensus with a consensus present"""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGG'
        sc=seqComparer( maxNs = 2,
                       reference=refSeq,
                       snpCeiling =10)

        with self.assertRaises(KeyError):
            res = sc.countDifferences_byKey(('AAAC','NNNC'))
  
        originals = [ 'AAAC', 'NNNC' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )


        # use a Tuple
        res = sc.countDifferences_byKey(('AAAC','NNNC'))
        self.assertEqual(res[2], None)
        
        refSeq='GGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        originals = [ 'AAAC', 'NNNC' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        res = sc.countDifferences_byKey(('AAAC','NNNC'))
        self.assertEqual(res[2], 0)

   
class test_seqComparer_45(unittest.TestCase):
    """ tests insertion of large sequences """
    def runTest(self):
        inputfile = "../reference/NC_000962.fasta"
        with open(inputfile, 'rt') as f:
            for record in SeqIO.parse(f,'fasta', alphabet=generic_nucleotide):
                    goodseq = str(record.seq)
                    badseq = ''.join('N'*len(goodseq))
                    originalseq = list(str(record.seq))
        sc=seqComparer( maxNs = 1e8,
                           reference=record.seq,
                           snpCeiling =100)
        n_pre =  0          
        guids_inserted = list()			
        for i in range(1,4):        #40
            
            seq = originalseq
            if i % 5 ==0:
                is_mixed = True
                guid_to_insert = "mixed_{0}".format(n_pre+i)
            else:
                is_mixed = False
                guid_to_insert = "nomix_{0}".format(n_pre+i)	
            # make i mutations at position 500,000
            
            offset = 500000
            nVariants = 0
            for j in range(i):
                mutbase = offset+j
                ref = seq[mutbase]
                if is_mixed == False:
                    nVariants +=1
                    if not ref == 'T':
                        seq[mutbase] = 'T'
                    if not ref == 'A':
                        seq[mutbase] = 'A'
                if is_mixed == True:
                        seq[mutbase] = 'N'					
            seq = ''.join(seq)
            
            if i % 11 == 0:
                seq = badseq        # invalid
                
            guids_inserted.append(guid_to_insert)			
            if not is_mixed:
                    print("Adding TB sequence {2} of {0} bytes with {1} Ns and {3} variants relative to ref.".format(len(seq), seq.count('N'), guid_to_insert, nVariants))
            else:
                    print("Adding mixed TB sequence {2} of {0} bytes with {1} Ns relative to ref.".format(len(seq), seq.count('N'), guid_to_insert))
                         
            self.assertEqual(len(seq), 4411532)		# check it's the right sequence
    
            c = sc.compress(seq)
            sc.persist(c, guid=guid_to_insert )
            if i % 5 == 0:
                sc.compress_relative_to_consensus(guid_to_insert)

class test_seqComparer_46(unittest.TestCase):
    """ tests the compression relative to a consensus with a consensus present.
    then adds more sequences, changing the consensus."""
    def runTest(self):
        
        # generate compressed sequences
        refSeq='GGGGGGGGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        
        originals = [ 'AAACACTGACTG','CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG','NNNCACTGACTG','ACTCACTGACTG','ACTCACTGACTG', 'TCTNACTGACTG' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        sc.compress_relative_to_consensus(guid = 'AAACACTGACTG', cutoff_proportion = 0.8)
        initial_consensi_keys = set(sc.consensi.keys())
        for original in originals:
            self.assertEqual(original, sc.uncompress(sc.seqProfile[original]))

        # add more changing the consensus by adding at T
        more_seqs = [ 'TAACACTGACTG','TCCCACTGACTG','TTTCACTGACTG','TGGCACTGACTG','TNNCACTGACTG','TCTCACTGACTG','TCTCACTGACTG', 'TCTNACTGACTG' ]
        for more_seq in more_seqs:   
            c = sc.compress(more_seq)
            sc.persist(c, guid=more_seq )

        sc.compress_relative_to_consensus(guid = 'AAACACTGACTG', cutoff_proportion = 0.8)
  
        for original in originals:
            self.assertEqual(original, sc.uncompress(sc.seqProfile[original]))
        for more_seq in more_seqs:
            self.assertEqual(more_seq, sc.uncompress(sc.seqProfile[more_seq]))

        # there should be one consensus
        later_consensi_keys = set(sc.consensi.keys())
        self.assertNotEqual(initial_consensi_keys, later_consensi_keys)
        self.assertEqual(len(later_consensi_keys), 1)
 
class test_seqComparer_47(unittest.TestCase):
    """ tests raise_error"""
    def runTest(self):
                # generate compressed sequences
        refSeq='GGGGGGGGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10)
        with self.assertRaises(ZeroDivisionError):
            sc.raise_error("token")

class test_seqComparer_mc1(unittest.TestCase):
    """ tests multithreaded search"""
    def runTest(self):
        refSeq='GGGGGGGGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10,
                       cpuCount = None)
        
        originals = [ 'AAACACTGACTG','CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG','NNNCACTGACTG','ACTCACTGACTG','ACTCACTGACTG', 'TCTNACTGACTG' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        expected_results = {'CCCCACTGACTG':3,
                            'TTTCACTGACTG':3,
                            'GGGCACTGACTG':3,
                            'NNNCACTGACTG':0,
                            'ACTCACTGACTG':2,
                            'TCTNACTGACTG':3,
                            'AAACACTGACTG':0}
        
        # test against selected guids
        res = sc.mcompare('AAACACTGACTG', ['CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG'])
        self.assertTrue(len(res),3)

        for (key1,key2,n,n1,n2,n3,s1,s2,s3) in res:
            expected_n = expected_results[key2]
            #print(key1,key2,n,n1,n2,n3,s1,s2,s3)
            self.assertEqual(expected_n, n)

        # test against all
        res = sc.mcompare('AAACACTGACTG')
        self.assertTrue(len(res),7)

        for (key1,key2,n,n1,n2,n3,s1,s2,s3) in res:
            expected_n = expected_results[key2]
            #print(key1,key2,n,n1,n2,n3,s1,s2,s3)
            self.assertEqual(expected_n, n)
        
class test_seqComparer_mc2(unittest.TestCase):
    """ tests single threaded search"""
    def runTest(self):
        refSeq='GGGGGGGGGGGG'
        sc=seqComparer( maxNs = 1e8,
                       reference=refSeq,
                       snpCeiling =10,
                       cpuCount = 1)
        
        originals = [ 'AAACACTGACTG','CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG','NNNCACTGACTG','ACTCACTGACTG','ACTCACTGACTG', 'TCTNACTGACTG' ]
        for original in originals:   
            c = sc.compress(original)
            sc.persist(c, guid=original )

        expected_results = {'CCCCACTGACTG':3,
                            'TTTCACTGACTG':3,
                            'GGGCACTGACTG':3,
                            'NNNCACTGACTG':0,
                            'ACTCACTGACTG':2,
                            'TCTNACTGACTG':3,
                            'AAACACTGACTG':0}
        
        # test against selected guids
        res = sc.mcompare('AAACACTGACTG', ['CCCCACTGACTG','TTTCACTGACTG','GGGCACTGACTG'])
        self.assertTrue(len(res),3)

        for (key1,key2,n,n1,n2,n3,s1,s2,s3) in res:
            expected_n = expected_results[key2]
            #print(key1,key2,n,n1,n2,n3,s1,s2,s3)
            self.assertEqual(expected_n, n)

        # test against all
        res = sc.mcompare('AAACACTGACTG')
        self.assertTrue(len(res),7)

        for (key1,key2,n,n1,n2,n3,s1,s2,s3) in res:
            expected_n = expected_results[key2]
            #print(key1,key2,n,n1,n2,n3,s1,s2,s3)
            self.assertEqual(expected_n, n)
        
class test_seqComparer_mc_benchmark(unittest.TestCase):
    """ not really a unit test; benchmarks the impact of multiple threads"""
    def runTest(self):
        
        for this_cpuCount in range(1,8):
            print("Cpus:", this_cpuCount)
            
            refSeq='GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG'
            sc=seqComparer( maxNs = 1e8,
                           reference=refSeq,
                           snpCeiling =10,
                           cpuCount = this_cpuCount)
            
            nAdded= 0
            guids = []
            c = sc.compress("".join("A"*len(refSeq)))
            for i in range(100000):
                nAdded +=1
                sc.persist(c, guid=str(nAdded) )
                guids.append(str(nAdded))
            print("added ",nAdded)
            
            # test against selected guids
            stime = datetime.datetime.now()
            res = sc.mcompare('1', guids)
            etime = datetime.datetime.now()
            delta = etime - stime
            print("compared with",this_cpuCount, delta)
            
        
