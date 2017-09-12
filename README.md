# Abstract
findNeighbour2 is a server application for investigating bacterial relatedness using reference-mapped data.
Accessible via RESTful webservices, findNeighbour2 maintains a sparse distance matrix in a database
for a sequence collection.  A maximum storable distance (e.g. 20 or 50 SNP) needs to be supplied.  Distances higher than this are
not cached.

findNeighbour2 supports incremental addition of samples, and, for a given sample, allows queries identifying similar sequences
with millisecond response times.

The inputs to the service are strings containing DNA sequence information, typically generated by mapping and basecalling, followed by storage in FASTA or other formats.
The service can be queried with strings containing DNA sequence information and a single nucleotide polymorphism threshold; it returns a list of similar samples.
The software is designed for, has been extensively tested with, mapped data from bacterial genome sequencing.

# Requirements
findNeighbour2 is written entirely in python and has three major components:
* webservice-server-rest, which is a Flask application implementing restful endpoints.  This calls the xmlrpc endpoints transparently.
* webservice-server, built on [web.py](http://webpy.org), which is an xmlrpc server.  The component doing this is webservice-server.py.
* seqComparer, a class which implements reference-based compression, in-memory storage of a compressed representation of the sequence,
fast sequence comparisons, and disc-based persistence. 

During development, findNeighbour2 was referred to as ElephantWalk2, and you may find references to ElephantWalk2 or EW2 in the code base.
# More information
[How to test it](doc/HowToTest.md)  
[Endpoints](doc/endpoints.md)

