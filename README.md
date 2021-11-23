# A newer product is available
Development is now focused a successor software,  findneighbour4.
This offers 
* backwards compatibility with findneighbour3  
* additional functionality
* faster operations and reduced RAM usage  
We therefore recommend that you use [findneighbour4](https://github.com/davidhwyllie/findNeighbour4) for new projects.

# Abstract
findNeighbour3 is a server application for investigating bacterial relatedness using reference-mapped data.
Accessible via RESTful webservices, findNeighbour3 maintains a sparse distance matrix in a database
for a set of sequences.

It has the following features:
* Allows incremental addition of new sequences to a collection via RESTful web services.  
* Automatically masks a pre-defined set of nucleotides in the reference mapped data.  Such sites are ignored in pairwise sequence computations.
* Maintains a sparse distance matrix of bacterial sequences using reference mapped sequence data.  Very large matrices can be efficiently and transparently stored.
* Returns pairwise distance matrices.
* Returns multiple sequence alignments.
* [Detects mixtures of different sequences](https://www.biorxiv.org/content/10.1101/681502v1).
* Automatically performs clustering to a range of SNV thresholds.
* Can detect, and appropriately cluster sequences in the presence of, inter-sample mixtures.
* Allows queries identifying similar sequences, cluster members, and multisequence alignments with  millisecond response times.
* Uses a highly compressed sequence representation, relying on compression to local reference, having first applied compression to the reference sequence to which mapping occurred.  This *double delta* technique aids storage of large numbers of sequences in RAM.
* Tracks memory usage, logging to database, during routine operation.
* Allow attachment of arbitrary metadata to each sequence, but the front end for this is not implemented.

It was produced as part of the [Modernising Medical Microbiology](http://modmedmicro.nsms.ox.ac.uk/) initiative, together with [Public Health England](https://www.gov.uk/government/organisations/public-health-england).

# Front end
There is a front end, *findNeighbour3 monitor*.   Although not required to run or use findNeighbour3 effectively, it helps to visualise server status and supports ad hoc queries.  In particular, it allows selecting and browsing of samples and clusters of samples in the server, including multisequence alignment, mixture detection, and depiction of their relationships.  

![findNeighbour3 monitor example page](https://davidhwyllie.github.io/FNMFINDNEIGHBOUR3/img/startup.PNG)  
The *findNeighbour3 monitor* is easy to use and to install.  See [details](doc/frontend.md).  
findNeighbour3 itself is accessed by [web services](doc/rest-routes.md). In general, these return json objects.

# Implementation and Requirements
findNeighbour3 is written entirely in python3.  
It operates on Windows and Linux environments.    
It uses mongodb as a storage layer.

# Access
The server can be accessed via RESTful web services from any language.
A python client (fn3client), which calls the REST endpoints and converts output into python objects, is also provided.

# Memory and disc usage
This depends on the kind of sequences stored.  For *M. tuberculosis*:

**Memory usage**   
* Memory usage is about 2G per 1,000 samples,   or 2M per sample. [calculated on Windows]  It scales linearly with sample numbers.
* 50,000 samples will use about 100G of RAM
* a machine with 2TB of RAM should be able to cope with 1M samples.  
   
**Database size**   
* database usage is about 0.2M (200kb) per sample.  This equates to about 5,000 samples per gigabyte.
* a free MongoDb test instance with MongoDb Atlas with 512M of storage will manage about 2,000 samples. Using the same provider, 
* an M20 EC2 instance (currently USD 0.22/hr) with 4G RAM and 20G disc storage will manage about 100,000 samples.

# Comparison with findNeighbour2
findNeighbour3 is a development of [findNeighbour2](https://github.com/davidhwyllie/findNeighbour2).
findNeighbour3's RESTful API is backwards compatible with that of findNeighbour2, but offers increased functionality.  
There are the following other differences:
* It uses additional compression (*double delta*), resulting in it needing about 30-50% of the memory required by findNeighbour2.
* It uses mongodb, not relational databases, for persistent storage.
* Queries are much faster for large numbers of samples
* It performs clustering.
* It is 'mixture-aware' and implements an approach for detecting mixed samples.
* Dependencies on linux-specific packages have been removed.
* It does not use any storage in a filesystem, except for logging.
* Internally, it has been refactored into four components, managing the web server, in-memory storage, on-disc storage, and clustering.
* It is only accessible via a RESTful endpoint.  The xmlrpc API included with findNeighbour2 has been removed.

# More information
[Set up and unit testing](doc/HowToTest.md)  
[Endpoints](doc/rest-routes.md)  
[Demonstrations using real and simulated data](doc/demos.md)  
[Integration tests](doc/integration.md)

# Publications
A publication describing findNeighbour3 implementation & performance is planned.  
A publication describing findNeighbour2 is in BMC Bioinformatics:  
*BugMat and FindNeighbour: command line and server applications for investigating bacterial relatedness*
DOI : 10.1186/s12859-017-1907-2 (https://dx.doi.org/10.1186/s12859-017-1907-2)  
The nature of the mixPORE (mixture detection algorithm) provided by the server, and its application to *M. tuberculosis* mixture detection is described [here](https://www.biorxiv.org/content/10.1101/681502v1).

# Large test data sets
Test data sets of *N. meningitidis*, *M. tuberculosis* and *S. enterica* data are available to download [here](https://ora.ox.ac.uk/objects/uuid:82ce6500-fa71-496a-8ba5-ba822b6cbb50).  These are .tar.gz files, to a total of 80GB.  
For the detection of mixtures, please see the additional test data sets [here](doc/demos_real.md).
