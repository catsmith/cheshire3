
from cheshire3.configParser import C3Object
from cheshire3.baseStore import SimpleStore
from cheshire3.baseObjects import Database
from cheshire3.recordStore import SimpleRecordStore
from cheshire3.documentStore import SimpleDocumentStore
from cheshire3.objectStore import SimpleObjectStore
from cheshire3.resultSetStore import SimpleResultSetStore

import irods

class IrodsStore(SimpleStore):

    cxn = None
    coll = None

    totalItems = 0
    totalWordCount = 0
    minWordCount = 0
    maxWordCount = 0
    meanWordCount = 0
    totalByteCount = 0
    minByteCount = 0
    maxByteCount = 0
    meanByteCount = 0
    lastModified = ''

    _possiblePaths = {'idNormalizer' : {'docs' : "Identifier for Normalizer to use to turn the data object's identifier into a suitable form for storing. Eg: StringIntNormalizer"},
                      'outIdNormalizer' : {'docs' : "Normalizer to reverse the process done by idNormalizer"},
                      'inWorkflow' : {'docs' : "Workflow with which to process incoming data objects."},
                      'outWorkflow' : {'docs' : "Workflow with which to process stored data objects when requested."}
                      }

    _possibleSettings = {'useUUID' : {'docs' : "Each stored data object should be assigned a UUID.", 'type': int, 'options' : "0|1"},
                         'digest' : {'docs' : "Type of digest/checksum to use. Defaults to no digest", 'options': 'sha|md5'},
                         'expires' : {'docs' : "Time after ingestion at which to delete the data object in number of seconds.", 'type' : int },
                         'storeDeletions' : {'docs' : "Maintain when an object was deleted from this store.", 'type' : int, 'options' : "0|1"}
                         }

    _possibleDefaults = {'expires': {"docs" : 'Default time after ingestion at which to delete the data object in number of seconds.  Can be overridden by the individual object.', 'type' : int}}
    


    def __init__(self, session, config, parent):
        C3Object.__init__(self, session, config, parent)
        self.cxn = None
        self.coll = None
        self._open(session)

        self.idNormalizer = self.get_path(session, 'idNormalizer', None)
        self.outIdNormalizer = self.get_path(session, 'outIdNormalizer', None)
        self.inWorkflow = self.get_path(session, 'inWorkflow', None)
        self.outWorkflow = self.get_path(session, 'outWorkflow', None)
        self.session = session

        self.useUUID = self.get_setting(session, 'useUUID', 0)
        self.expires = self.get_default(session, 'expires', 0)

    def get_metadataTypes(self, session):
        return {'totalItems' : long,
                'totalWordCount' : long,
                'minWordCount' : long,
                'maxWordCount' : long,
                'totalByteCount' : long,
                'minByteCount' : long,
                'maxByteCount' : long,
                'lastModified' : str}

    def _open(self, session):
        # XXX Find location from config
        path = 'cheshire3'

        # connect to iRODS
        myEnv, status = irods.getRodsEnv()
        conn, errMsg = irods.rcConnect(myEnv.getRodsHost(), myEnv.getRodsPort(), 
                                       myEnv.getRodsUserName(), myEnv.getRodsZone())
        status = irods.clientLogin(conn)
        if status:
            raise ConfigFileException("Cannot connect to iRODS: (%s) %s" % (status, errMsg))

        c = irods.irodsCollection(conn, myEnv.getRodsHome())
        self.cxn = conn
        self.coll = c

        dirs = c.getSubCollections()
        if not path in dirs:
            c.createCollection(path)
        c.openCollection(path)

        # now look for object's storage area
        if (isinstance(self.parent, Database)):
            sc = self.parent.id
            dirs = c.getSubCollections()
            if not sc in dirs:
                c.createCollection(sc)
            c.openCollection(sc)

        dirs = c.getSubCollections()
        if not self.id in dirs:
            c.createCollection(self.id)
        c.openCollection(self.id)

        # Fetch user metadata
        myMetadata = self.get_metadataTypes(session)
        umd = c.getUserMetadata()
        umdHash = {}
        for u in umd:
            umdHash[u[0]] = u[1:]            
        for md in myMetadata:
            try:
                setattr(self, md, myMetadata[md](umdHash[md][0]))
            except KeyError:
                # hasn't been set yet
                pass

        if self.totalItems != 0:
            self.meanWordCount = self.totalWordCount / self.totalItems
            self.meanByteCount = self.totalByteCount / self.totalItems
        else:
            self.meanWordCount = 1
            self.meanByteCount = 1

        
    def _close(self, session):
        irods.rcDisconnect(self.cxn)
        self.cxn = None
        self.coll = None


    def _queryMeta(self, q):
        genQueryInp = irods.genQueryInp_t()
        

    def commit_metadata(self, session):
        mymd = self.get_metadataTypes(session)
        # need to delete values first
        umd = self.coll.getUserMetadata()
        umdHash = {}
        for u in umd:
            umdHash[u[0]] = u[1:]                    
        for md in mymd:
            try:
                self.coll.rmUserMetadata(md, umdHash[md][0])
            except KeyError:
                # not been set yet
                pass
            # should this check for unicode and encode('utf-8') ?
            self.coll.addUserMetadata(md, str(getattr(self, md)))

    def begin_storing(self, session):
        if not this.cxn:
            self._open(session)
        return None

    def commit_storing(self, session):
        self.commit_metadata(session)
        self._close()
        return None

    def get_dbSize(self, session):
        return self.coll.getLenObjects()

    def delete_data(self, session, id):
        # delete data stored against id
        if (self.idNormalizer != None):
            id = self.idNormalizer.process_string(session, id)
        elif type(id) == unicode:
            id = id.encode('utf-8')
        else:
            id = str(id)

        self.coll.delete(id)
        # all metadata stored on object, no need to delete from elsewhere

        # Maybe store the fact that this object used to exist.
        if self.get_setting(session, 'storeDeletions', 0):
            now = datetime.datetime.now(dateutil.tz.tzutc()).strftime("%Y-%m-%dT%H:%M:%S%Z").replace('UTC', 'Z')            
            data = "\0http://www.cheshire3.org/status/DELETED:%s" % now
            f = self.coll.create(id)
            f.write(data)
            f.close()
        return None

    def fetch_data(self, session, id):
        # return data stored against id
        if (self.idNormalizer != None):
            id = self.idNormalizer.process_string(session, id)
        elif type(id) == unicode:
            id = id.encode('utf-8')
        else:
            id = str(id)

        f = self.coll.open(id)        
        data = f.read()
        f.close()

        if data and data[:41] == "\0http://www.cheshire3.org/status/DELETED:":
            data = DeletedObject(self, id, data[41:])

        if data and self.expires:
            expires = self.generate_expires(session)
            f.addUserMetadata('expires', expires)
        return data

    def store_data(self, session, id, data, metadata):
        dig = metadata.get('digest', "")
        if 0 and dig:
            raise NotImplementedError()
            if self.cxn.queryUserMetadata('select data_name where ...'):
                raise ObjectAlreadyExistsException(exists)

        if id == None:
            id = self.generate_id(session)
        if (self.idNormalizer != None):
            id = self.idNormalizer.process_string(session, id)
        elif type(id) == unicode:
            id = id.encode('utf-8')
        else:
            id = str(id)

        if type(data) == unicode:
            data = data.encode('utf-8')
        f = self.coll.create(id)
        f.write(data)
        f.close()

        # store metadata with object
        for (m, val) in metadata.iteritems():
            f.addUserMetadata(m, str(val))
        return None

    def fetch_metadata(self, session, id, mType):
        # open irodsFile and get metadata from it
        f = self.coll.open(id)
        umd = f.getUserMetadata()
        for x in umd:
            if x[0] == mType:
                return x[1]
        
    def store_metadata(self, session, id, mType, value):
        # store value for mType metadata against id
        f = self.coll.open(id)
        f.addUserMetadata(mType, value)
        f.close()
    
    def clean(self, session):
        # delete expired data objects
        # self.cxn.query('select data_name from bla where expire < now')
        raise NotImplementedError

    def clear(self, session):
        # delete all objects
        for o in self.coll.getObjects():
            self.coll.delete(o)
        return None
            
    def flush(self, session):
        # ensure all data is flushed to disk
        # don't think there's an equivalent
        return None


# hooray for multiple inheritance!

class IrodsRecordStore(SimpleRecordStore, IrodsStore):
    def __init__(self, session, config, parent):
        IrodsStore.__init__(self, session, config, parent)
        SimpleRecordStore.__init__(self, session, config, parent)

class IrodsDocumentStore(SimpleDocumentStore, IrodsStore):
    def __init__(self, session, config, parent):
        IrodsStore.__init__(self, session, config, parent)
        SimpleDocumentStore.__init__(self, session, config, parent)
        
class IrodsObjectStore(SimpleObjectStore, IrodsStore):
    def __init__(self, session, config, parent):
        IrodsStore.__init__(self, session, config, parent)
        SimpleObjectStore.__init__(self, session, config, parent)

class IrodsResultSetStore(SimpleResultSetStore, IrodsStore):
    def __init__(self, session, config, parent):
        IrodsStore.__init__(self, session, config, parent)
        SimpleResultSetStore.__init__(self, session, config, parent)


class IrodsDirectoryDocumentStream(MultipleDocumentStream):

    def find_documents(self, session, cache=0):
        # given a location in irods, go there and descend looking for files

        myEnv, status = irods.getRodsEnv()
        conn, errMsg = irods.rcConnect(myEnv.getRodsHost(), myEnv.getRodsPort(), 
                                       myEnv.getRodsUserName(), myEnv.getRodsZone())
        status = irods.clientLogin(conn)
        if status:
            raise ConfigFileException("Cannot connect to iRODS: (%s) %s" % (status, errMsg))

        home = myEnv.getRodsHome()
        c = irods.irodsCollection(conn, home)
        dirs = c.getSubCollections()

        # check if abs path to home dir
        if self.streamLocation.startswith(home):
            self.streamLocation = self.streamLocation[len(home):]
            if self.streamLocation[0] == "/":
                self.streamLocation = self.streamLocation[1:]

        colls = self.streamLocation.split('/')
        for cln in colls:
            c.openCollection(cln)
        
        # now at top of data... recursively look for files


        for root, dirs, files in os.walk(self.streamLocation):
            for d in dirs:
                if os.path.islink(os.path.join(root, d)):
                    for root2, dirs2, files2 in os.walk(os.path.join(root,d)):
                        files2.sort()
                        files2 = [os.path.join(root2, x) for x in files2]
                        # Psyco Map Reduction
                        # files2 = map(lambda x: os.path.join(root2, x), files2)
                        for f in self._processFiles(session, files2, cache):
                            yield f
            files.sort()
            files = [os.path.join(root, x) for x in files]
            # files = map(lambda x: os.path.join(root, x), files)
            for f in self._processFiles(session, files, cache):
                yield f

