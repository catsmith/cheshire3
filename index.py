
from baseObjects import Index, Document
from configParser import C3Object
from utils import elementType, getFirstData, verifyXPaths, flattenTexts
from c3errors import ConfigFileException
import re, types, sys, os, struct, time
from record import SaxRecord, DomRecord
from resultSet import SimpleResultSet, SimpleResultSetItem
from workflow import CachingWorkflow
from PyZ3950 import CQLParser, SRWDiagnostics
import codecs
from baseObjects import Session

try:
    import termine
except:
    pass

from xpathObject import XPathObject


class IndexIter(object):
    index = None
    session = None

    def __init__(self, index):
        self.index = index
        self.indexStore = index.indexStore
        self.session = Session()
        # populate with first term
        self.nextData = self.indexStore.fetch_termList(self.session, self.index, "", 1)[0]

    def __iter__(self):
        return self

    def next(self):
        try:
            d = self.nextData
            if not d:
                raise StopIteration()
            if d[-1] == 'last':
                self.nextData = ""
            else:
                try:
                    self.nextData = self.indexStore.fetch_termList(self.session, self.index, d[0], 2)[1]
                except IndexError:
                    self.nextData = ""
            return self.index.construct_resultSet(self.session, d[1], queryHash={'text':d[0], 'occurences': 1, 'positions' : []})
        except:
            # fail safe
            raise StopIteration()

    def jump(self, position):
        # Jump to this position
        self.nextData = self.indexStore.fetch_termList(self.session, self.index, position, 1)[0]
        return self.index.construct_resultSet(self.session, self.nextData[1], queryHash={'text':self.nextData[0], 'occurences': 1, 'positions' : []})



class SimpleIndex(Index):
    sources = {}
    xPathAllAbsolute = 1
    xPathAttributesRequired = []
    xPathsNormalized = {}
    currentFullPath = []
    currentPath = []
    storeOrig = 0
    canExtractSection = 1

    indexingTerm = ""
    indexingData = []

    _possiblePaths = {'indexStore' : {"docs" : "IndexStore identifier for where this index is stored"}
                      , 'termIdIndex' : {"docs" : "Alternative index object to use for termId for terms in this index."}
                      , 'termineDb' : {"docs" : "Path to the 'termine' database of term scores for this index. No, Termine does not come with C3 by default."}
                      , 'tempPath' : {"docs" : "Path to a directory where temporary files will be stored during batch mode indexing"}
                      }

    _possibleSettings = {'cori_constant0' : {"docs" : ""}
                         , 'cori_constant1' : {"docs" : ""}
                         , 'cori_constant2' : {"docs" : ""}
                         , 'lr_constant0' : {"docs" : ""}
                         , 'lr_constant1' : {"docs" : ""}
                         , 'lr_constant2' : {"docs" : ""}
                         , 'lr_constant3' : {"docs" : ""}
                         , 'lr_constant4' : {"docs" : ""}
                         , 'lr_constant5' : {"docs" : ""}
                         , 'lr_constant6' : {"docs" : ""}
                         , 'noIndexDefault' : {"docs" : "If true, the index should not be called from db.index_record()", "type" : int, "options" : "0|1"}
                         , 'noUnindexDefault' : {"docs" : "If true, the index should not be called from db.unindex_record()", "type" : int, "options" : "0|1"}
                         , 'sortStore' : {"docs" : "Should the index build a sort store"}
                         , 'vectors' : {"docs" : "Should the index store vectors (doc -> list of termIds"}
                         , 'proxVectors' : {"docs" : "Should the index store vectors that also maintain proximity for their terms"}
                         ,  'minimumSupport' : {"docs" : ""}
                         , 'vectorMinGlobalFreq' : {"docs" : ""}
                         , 'vectorMaxGlobalFreq' : {"docs" : ""}
                         , 'vectorMinGlobalOccs' : {"docs" : ""}
                         , 'vectorMaxGlobalOccs' : {"docs" : ""}
                         , 'vectorMinLocalFreq' : {"docs" : ""}
                         , 'vectorMaxLocalFreq' : {"docs" : ""}
                         , 'freqList' : {'docs' : '', 'options' : 'rec|occ|rec occ|occ rec'}
                         , 'longSize' : {"docs" : "Size of a long integer in this index's underlying data structure (eg to migrate between 32 and 64 bit platforms)"}
                         , 'recordStoreSizes' : {"docs" : ""},
                         'maxVectorCacheSize' : {'docs' : "Number of terms to cache when building vectors", 'type' :int}
                         }


    def _handleConfigNode(self, session, node):
        # Source
        if (node.localName == "source"):
            modes = node.getAttributeNS(None, 'mode')
            if not modes:
                modes = [u'data']
            else:
                modes = modes.split('|')
            process = None
            preprocess = None
            xp = None
            for child in node.childNodes:
                if child.nodeType == elementType:
                    if child.localName == "xpath":
                        if xp == None:
                            ref = child.getAttributeNS(None, 'ref')
                            if ref:
                                xp = self.get_object(session, ref)
                            else:
                                xp = XPathObject(session, node, self)
                                xp._handleConfigNode(session, node)
                    elif child.localName == "preprocess":
                        # turn preprocess chain to workflow
                        ref = child.getAttributeNS(None, 'ref')
                        if ref:
                            preprocess = self.get_object(session, ref)
                        else:
                            child.localName = 'workflow'
                            preprocess = CachingWorkflow(session, child, self)
                            preprocess._handleConfigNode(session, child)
                    elif child.localName == "process":
                        # turn xpath chain to workflow
                        ref = child.getAttributeNS(None, 'ref')
                        if ref:
                            process = self.get_object(session, ref)
                        else:
                            try:
                                child.localName = 'workflow'
                            except:
                                # 4suite dom sets read only
                                newTop = child.ownerDocument.createElementNS(None, 'workflow')
                                for kid in child.childNodes:
                                    newTop.appendChild(kid)
                                child = newTop
                            process = CachingWorkflow(session, child, self)
                            process._handleConfigNode(session, child)

            for m in modes:
                self.sources.setdefault(m, []).append((xp, process, preprocess))

    def __init__(self, session, config, parent):
        self.sources = {}
        self.xPathAttributesRequired = []
        self.xPathsNormalized = {}
        self.xPathAllAbsolute = 1
        self.indexingTerm = ""
        self.indexingData = []

        self.maskList = ['*', '?', '^']
        self.caretRe = re.compile(r'(?<!\\)\^')
        self.qmarkRe = re.compile(r'(?<!\\)\?')
        self.astxRe = re.compile(r'(?<!\\)\*')

        Index.__init__(self, session, config, parent)
        lss = self.get_setting(session, 'longSize')
        if lss:
            self.longStructSize = int(lss)
        else:
            self.longStructSize = len(struct.pack('L', 1))
        self.recordStoreSizes = self.get_setting(session, 'recordStoreSizes', 0)

        # We need a Store object
        iStore = self.get_path(session, 'indexStore', None)
        self.indexStore = iStore

        if (iStore == None):
            raise(ConfigFileException("Index (%s) does not have an indexStore." % (self.id)))
        elif not iStore.contains_index(session, self):
            iStore.create_index(session, self)

        self.resultSetClass = SimpleResultSet
        self.recordStore = ""

    def __iter__(self):
        return IndexIter(self)


    def _locate_firstMask(self, term, start=0):
        try:
            return min([term.index(x, start) for x in self.maskList])
        except ValueError:
            # one or more are not found (i.e. == -1)
            firstMaskList = [term.find(x, start) for x in self.maskList]
            firstMaskList.sort()
            firstMask = firstMaskList.pop(0)
            while len(firstMaskList) and firstMask < 0:
                firstMask = firstMaskList.pop(0)
            return firstMask

    def _regexify_wildcards(self, term):
        term = term.replace('.', r'\.')          # escape existing special regex chars
        term = term[0] + self.caretRe.sub(r'\^', term[1:-1]) + term[-1] 
        term = self.qmarkRe.sub('.', term)
        term = self.astxRe.sub('.*', term)
        if (term[-1] == '^') and (term[-2] != '\\'):
            term = term[:-1]
        return term + '$'


    def _processRecord(self, session, record, source):
        (xpath, process, preprocess) = source
        if preprocess:
            record = preprocess.process(session, record)
        if xpath:
            rawlist = xpath.process_record(session, record)
            processed = process.process(session, rawlist)
        else:
            processed = process.process(session, record)
        return processed


    def extract_data(self, session, record):
        processed = self._processRecord(session, record, self.sources[u'data'][0])
        if processed:
            keys = processed.keys()
            keys.sort()
            return keys[0]
        else:
            return None

    def index_record(self, session, record):
        # First extract simple paths, the majority of cases
        p = self.permissionHandlers.get('info:srw/operation/2/index', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to add to index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to add to index %s" % self.id)
        for src in self.sources[u'data']:
            processed = self._processRecord(session, record, src)
            self.indexStore.store_terms(session, self, processed, record)
        return record

    def delete_record(self, session, record):
        # Extract terms, and remove from store
        p = self.permissionHandlers.get('info:srw/operation/2/unindex', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to remove from index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to remove from index %s" % self.id)
        istore = self.get_path(session, 'indexStore')

        if self.get_setting(session, 'vectors', 0):
            # use vectors to unindex instead of reprocessing
            # faster, only way for 'now' metadata.
            vec = self.fetch_vector(session, record)
            # [totalUniqueTerms, totalFreq, [(tid, freq)+]]
            processed = {}
            for (t,f) in vec[2]:
                term = self.fetch_termById(session, t)
                processed[term] = {'occurences' : f}
            #print "VECTOR processed: %r" % processed
            if istore != None:
                istore.delete_terms(session, self, processed, record)
        else:
            for src in self.sources[u'data']:
                processed = self._processRecord(session, record, src)
                #print "NORMAL processed: %r" % processed
                if (istore != None):
                    istore.delete_terms(session, self, processed, record)
                
    def begin_indexing(self, session):
        # Find all indexStores
        p = self.permissionHandlers.get('info:srw/operation/2/index', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to add to index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to add to index %s" % self.id)
        stores = []
        istore = self.get_path(session, 'indexStore')
        if (istore != None and not istore in stores):
            stores.append(istore)
        for s in stores:
            s.begin_indexing(session, self)


    def commit_indexing(self, session):
        p = self.permissionHandlers.get('info:srw/operation/2/index', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to add to index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to add to index %s" % self.id)
        stores = []
        istore = self.get_path(session, 'indexStore')
        if (istore != None and not istore in stores):
            stores.append(istore)
        for s in stores:
            s.commit_indexing(session, self)

    def search(self, session, clause, db):
        # Final destination. Process Term.
        p = self.permissionHandlers.get('info:srw/operation/2/search', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to search index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to search index %s" % self.id)

        res = {}
        # src = (xp, processwf, preprocesswf)
        # try to get process for relation/modifier, failing that relation, fall back to that used for data
        for src in self.sources.get(clause.relation.toCQL(), self.sources.get(clause.relation.value, self.sources[u'data'])):
            res.update(src[1].process(session, [[clause.term.value]]))

        store = self.get_path(session, 'indexStore')
        matches = []
        rel = clause.relation

        if (rel.value in ['any', 'all', '=', 'exact'] and (rel.prefix == 'cql' or rel.prefixURI == 'info:srw/cql-context-set/1/cql-v1.1')):
            for k, qHash in res.iteritems():
                if k[0] == '^': k = k[1:]      
                firstMask = self._locate_firstMask(k)
                while (firstMask > 0) and (k[firstMask-1] == '\\'):
                    firstMask = self._locate_firstMask(k, firstMask+1)
                # TODO: slow regex e.g. if first char is *
                if (firstMask > 0):
                    startK = k[:firstMask]
                    try: nextK = startK[:-1] + chr(ord(startK[-1])+1)
                    except IndexError:
                        # we need to do something clever - seems they want left truncation
                        # FIXME: need to do something cleverer than this!
                        termList =  store.fetch_termList(session, self, startK, 0, '>=')
                    else:
                        termList =  store.fetch_termList(session, self, startK, 0, '>=', end=nextK)
                    maskBase = SimpleResultSet(session)
                    maskClause = clause
                    maskClause.relation.value = 'any'
                    if (firstMask < len(k)-1) or (k[firstMask] in ['?', '^']):
                        # not simply right hand truncation
                        k = self._regexify_wildcards(k)
                        kRe = re.compile(k)
                        mymatch = kRe.match
                        termList = filter(lambda t: mymatch(t[0]), termList)

                    try:
                        maskBase = maskBase.combine(session, [self.construct_resultSet(session, t[1], qHash) for t in termList], maskClause, db)
                    except:
                        pass
                    matches.append(maskBase)
                    
                elif (firstMask == 0):
                    # TODO: come up with some way of returning results, even though they're trying to mask the first character
                    # above implementation would be incredibly slow for this case.
                    pass
                else:
                    term = store.fetch_term(session, self, k)
                    s = self.construct_resultSet(session, term, qHash)
                    matches.append(s)
        elif (clause.relation.value in ['>=', '>', '<', '<=']):
            if (len(res) <> 1):
                d = SRWDiagnostics.Diagnostic24()
                d.details = "%s %s" % (clause.relation.toCQL(), clause.term.value)
                raise d
            else:
                termList = store.fetch_termList(session, self, res.keys()[0], 0, clause.relation.value)
                for t in termList:
                    matches.append(self.construct_resultSet(session, t[1]))
        elif (clause.relation.value == "within"):
            if (len(res) <> 2):
                d = SRWDiagnostics.Diagnostic24()
                d.details = '%s "%s"' % (clause.relation.toCQL(), clause.term.value)
                raise d
            else:
                termList = store.fetch_termList(session, self, res.keys()[0], end=res.keys()[1])
                matches.extend([self.construct_resultSet(session, t[1]) for t in termList])

        else:
            d = SRWDiagnostics.Diagnostic24()
            d.details = '%s "%s"' % (clause.relation.toCQL(), clause.term.value)
            raise d

        base = self.resultSetClass(session, [], recordStore=self.recordStore)
        base.recordStoreSizes = self.recordStoreSizes
        if not matches:
            return base
        else:
            rs = base.combine(session, matches, clause, db)
            # maybe insert termine stuff
            tdb = self.get_path(session, 'termineDb')
            if tdb:
                rs.termWeight = 0
                for k in res:
                    w = termine.fetch_weight(session, k, tdb)
                    rs.termWeight += w                    
            return rs

    def scan(self, session, clause, numReq, direction=">="):
        # Process term.
        p = self.permissionHandlers.get('info:srw/operation/2/scan', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to scan index %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to scan index %s" % self.id)

        res = {}
        for src in self.sources.get(clause.relation.toCQL(), self.sources.get(clause.relation.value, self.sources[u'data'])):
            res.update(src[1].process(session, [[clause.term.value]]))

        if (len(res) <> 1):
            d = SRWDiagnostics.Diagnostic24()
            d.details = "%s" % (clause.term.value)
            raise d
        store = self.get_path(session, 'indexStore')
        tList = store.fetch_termList(session, self, res.keys()[0], numReq=numReq, relation=direction, summary=1)
        # list of (term, occs)
        return tList

    def serialise_terms(self, termid, terms, recs=0, occs=0):
        # in: list of longs
        if not recs:
            recs = len(terms) / 3
            occs = sum(terms[2::3])
        fmt = 'lll' * (recs + 1)
        params = [fmt, termid, recs, occs] + terms
        return struct.pack(*params)
        
    def deserialise_terms(self, data, prox=1):
        fmt = 'lll' * (len(data) / (3 * self.longStructSize))
        return struct.unpack(fmt, data)

    def calc_sectionOffsets(self, session, start, num, dlen=0):
        #tid, recs, occs, (store, rec, freq)+
        a = (self.longStructSize * 3) + (self.longStructSize *start * 3)
        b = (self.longStructSize * 3 * num)
        return [(a,b)]

    def merge_terms(self, structTerms, newTerms, op="replace", recs=0, occs=0):
        # structTerms = output of deserialiseTerms
        # newTerms = flat list
        # op = replace, add, delete
        # recs, occs = total recs/occs in newTerms

        (termid, oldTotalRecs, oldTotalOccs) = structTerms[0:3]
        structTerms = list(structTerms[3:])

        if op == 'add':
            structTerms.extend(newTerms)
            if recs:
                trecs = oldTotalRecs + recs
                toccs = oldTotalOccs + occs
            else:
                trecs = oldTotalRecs + len(newTerms) / 3
                toccs = oldTotalOccs + sum(newTerms[2::3])
        elif op == 'replace':
            for n in range(0,len(newTerms),3):
                docid = newTerms[n]
                storeid = newTerms[n+1]                
                replaced = 0
                for x in range(3, len(structTerms), 3):
                    if structTerms[x] == docid and structTerms[x+1] == storeid:
                        structTerms[x+2] == newTerms[n+2]
                        replaced = 1
                        break
                if not replaced:
                    structTerms.extend([docid, storeid, newTerms[n+2]])

            trecs = len(structTerms) / 3
            toccs = sum(structTerms[2::3])
        elif op == 'delete':            
            for n in range(0,len(newTerms),3):
                docid = newTerms[n]
                storeid = newTerms[n+1]                
                for x in range(0, len(structTerms), 3):
                    if structTerms[x] == docid and structTerms[x+1] == storeid:
                        del structTerms[x:x+3]
                        break
            trecs = len(structTerms) / 3
            toccs = sum(structTerms[2::3])
                    
        merged = [termid, trecs, toccs] + structTerms
        return merged

    def construct_item(self, session, term, rsitype="SimpleResultSetItem"):
        # in: single triple
        # out: resultSetItem
        # Need to map recordStore and docid at indexStore
        return self.indexStore.create_item(session, term[0], term[1], term[2], rsitype)

    def construct_resultSet(self, session, terms, queryHash={}):
        # in: unpacked
        # out: resultSet
        l = len(terms)        
        ci = self.indexStore.create_item

        s = self.resultSetClass(session, [])
        rsilist = []
        for t in range(3,len(terms),3):
            item = ci(session, terms[t], terms[t+1], terms[t+2])
            item.resultSet = s
            rsilist.append(item)
        s.fromList(rsilist)
        s.index = self
        if queryHash:
            s.queryTerm = queryHash['text']
            s.queryFreq = queryHash['occurences']
        if (terms):
            s.termid = terms[0]
            s.totalRecs = terms[1]
            s.totalOccs = terms[2]
        else:
            s.totalRecs = 0
            s.totalOccs = 0
        return s





    # pass-throughs to indexStore

    def store_terms(self, session, data, record):
        self.indexStore.store_terms(session, self, data, record)

    def fetch_term(self, session, term, summary=False, prox=True):
        return self.indexStore.fetch_term(session, self, term, summary, prox)

    def fetch_termList(self, session, term, numReq=0, relation="", end="", summary=0):
        return self.indexStore.fetch_termList(session, self, term, numReq, relation, end, summary)

    def fetch_termById(self, session, termId):
        return self.indexStore.fetch_termById(session, self, termId)

    def fetch_vector(self, session, rec, summary=False):
        return self.indexStore.fetch_vector(session, self, rec, summary)

    def fetch_proxVector(self, session, rec, elemId=-1):
        return self.indexStore.fetch_proxVector(session, self, rec, elemId)

    def fetch_summary(self, session):
	return self.indexStore.fetch_summary(session, self)

    def fetch_termFrequencies(self, session, which='rec', position=-1, number=100, direction="<="):
        return self.indexStore.fetch_termFrequencies(session, self, which, position, number, direction)



    

class ProximityIndex(SimpleIndex):
    """ Need to use prox extracter """

    canExtractSection = 0
    _possibleSettings = {'nProxInts' : {'docs' : "", 'type' : int}}

    def __init__(self, session, config, parent):
        SimpleIndex.__init__(self, session, config, parent)
        self.nProxInts = self.get_setting(session, 'nProxInts', 2)

    def serialise_terms(self, termid, terms, recs=0, occs=0):
        # in: list of longs
        fmt = 'l' * (len(terms) + 3)
        params = [fmt, termid, recs, occs] + terms
        try:
            val =  struct.pack(*params)
        except:
            print "%s failed to pack: %r" % (self.id, params)
            raise
        return val
        
    def deserialise_terms(self, data, prox=1):
        fmt = 'L' * (len(data) / self.longStructSize)
        flat = struct.unpack(fmt, data)
        (termid, totalRecs, totalOccs) = flat[:3]
        idx = 3
        docs = [termid, totalRecs, totalOccs]
        while idx < len(flat):
            doc = list(flat[idx:idx+3])
            nidx = idx + 3 + (doc[2]*self.nProxInts)
            doc.extend(flat[idx+3:nidx])
            idx = nidx
            docs.append(doc)
        return docs

    def merge_terms(self, structTerms, newTerms, op="replace", recs=0, occs=0):
        # in: struct: deserialised, new: flag
        # out: flat
        
        (termid, oldTotalRecs, oldTotalOccs) = structTerms[0:3]
        structTerms = list(structTerms[3:])

        if op == 'add':
            # flatten
            terms = []
            for t in structTerms:
                terms.extend(t)
            terms.extend(newTerms)
            structTerms = terms
            if recs != 0:
                trecs = oldTotalRecs + recs
                toccs = oldTotalOccs + occs
            else:
                # ...
                trecs = oldTotalRecs + len(newTerms)
                toccs = oldTotalOccs 
                for t in newTerms:            
                    toccs = toccs + t[2]
                raise ValueError("FIXME:  mergeTerms needs recs/occs params")
        elif op == 'replace':

            recs = [(x[0], x[1]) for x in structTerms]
            newOccs = 0

            idx = 0
            while idx < len(newTerms):
                end = idx + 3 + (newTerms[idx+2]*self.nProxInts)
                new = list(newTerms[idx:end])
                idx = end
                docid = new[0]
                storeid = new[1]                                
                if (docid, storeid) in recs:
                    loc = recs.index((docid, storeid))
                    # subtract old occs
                    occs = structTerms[loc][2]
                    newOccs -= occs
                    structTerms[loc] = new
                else:
                    structTerms.append(new)
                newOccs += new[2]
            trecs = len(structTerms)
            toccs = oldTotalOccs + newOccs            
            # now flatten structTerms
            n = []
            for s in structTerms:
                n.extend(s)
            structTerms = n
                
        elif op == 'delete':            
            delOccs = 0
            idx = 0
            while idx < len(newTerms):
                doc = list(newTerms[idx:idx+3])
                idx = idx + 3 + (doc[2]*self.nProxInts)
                for x in range(len(structTerms)):
                    old = structTerms[x]
                    if old[0] == doc[0] and old[1] == doc[1]:
                        delOccs = delOccs + old[2]
                        del structTerms[x]
                        break
            trecs = len(structTerms) -3
            toccs = oldTotalOccs - delOccs
            # now flatten
            terms = []
            for t in structTerms:
                terms.extend(t)
            structTerms = terms
                    
        merged = [termid, trecs, toccs]
        merged.extend(structTerms)
        return merged

    def construct_item(self, session, term):
        # in: single triple
        # out: resultSetItem
        # Need to map recordStore and docid at indexStore
        item = self.indexStore.create_item(session, term[0], term[1], term[2])
        item.proxInfo = term[3:]
        return item

    def construct_resultSet(self, session, terms, queryHash={}):
        # in: unpacked
        # out: resultSet

        rsilist = []
        ci = self.indexStore.create_item
        s = self.resultSetClass(session, [])
        for t in terms[3:]:
            item = ci(session, t[0], t[1], t[2])
            item.proxInfo = t[3:]
            item.resultSet = s
            rsilist.append(item)
        s.fromList(rsilist)
        s.index = self
        if queryHash:
            s.queryTerm = queryHash['text']
            s.queryFreq = queryHash['occurences']
            s.queryPositions = []
            # not sure about this nProxInts??
            for x in queryHash['positions'][1::self.nProxInts]:
                s.queryPositions.append(x)
        if (terms):
            s.termid = terms[0]
            s.totalRecs = terms[1]
            s.totalOccs = terms[2]
        else:
            s.totalRecs = 0
            s.totalOccs = 0
        return s



class RangeIndex(SimpleIndex):
    """ Need to use a RangeExtracter """
    # 1 3 should make 1, 2, 3
    # a c should match a* b* c
    # unsure about this - RangeIndex only necessary for 'encloses' queries - John
    # also appropriate for 'within', so implememnted - John

    def search(self, session, clause, db):
        # check if we can just use SimpleIndex.search
        if (clause.relation.value not in ['encloses', 'within']):
            return SimpleIndex.search(self, session, clause, db)
        else:
            p = self.permissionHandlers.get('info:srw/operation/2/search', None)
            if p:
                if not session.user:
                    raise PermissionException("Authenticated user required to search index %s" % self.id)
                okay = p.hasPermission(session, session.user)
                if not okay:
                    raise PermissionException("Permission required to search index %s" % self.id)
    
            # Final destination. Process Term.
            res = {}    
            # try to get process for relation/modifier, failing that relation, fall back to that used for data
            for src in self.sources.get(clause.relation.toCQL(), self.sources.get(clause.relation.value, self.sources[u'data'])):
                res.update(src[1].process(session, [[clause.term.value]]))
    
            store = self.get_path(session, 'indexStore')
            matches = []
            rel = clause.relation

            if (len(res) <> 1):
                d = SRWDiagnostics.Diagnostic24()
                d.details = "%s %s" % (clause.relation.toCQL(), clause.term.value)
                raise d

            keys = res.keys()[0].split('\t', 1)
            startK = keys[0]
            endK = keys[1]
            if clause.relation.value == 'encloses':
                # RangeExtracter should already return the range in ascending order
                termList = store.fetch_termList(session, self, startK, relation='<')
                termList = filter(lambda t: endK < t[0].split('\t', 1)[1], termList)
                matches.extend([self.construct_resultSet(session, t[1]) for t in termList])
            elif clause.relation.value == 'within':
                termList = store.fetch_termList(session, self, startK, end=endK)
                termList = filter(lambda t: endK > t[0].split('\t', 1)[1], termList)
                matches.extend([self.construct_resultSet(session, t[1]) for t in termList])
            else:
                # this just SHOULD NOT have happened!...
                d = SRWDiagnostics.Diagnostic24()
                d.details = '%s "%s"' % (clause.relation.toCQL(), clause.term.value)
                raise d
    
            base = self.resultSetClass(session, [], recordStore=self.recordStore)
            base.recordStoreSizes = self.recordStoreSizes
            if not matches:
                return base
            else:
                rs = base.combine(session, matches, clause, db)
                # maybe insert termine stuff
                tdb = self.get_path(session, 'termineDb')
                if tdb:
                    rs.termWeight = 0
                    for k in res:
                        w = termine.fetch_weight(session, k, tdb)
                        rs.termWeight += w                    
                return rs


from utils import SimpleBitfield
from resultSet import BitmapResultSet

class BitmapIndex(SimpleIndex):
    # store as hex -- fast to generate, 1 byte per 4 bits.
    # eval to go from hex to long for bit manipulation

    _possiblePaths = {'recordStore' : {"docs" : "The recordStore in which the records are kept (as this info not maintained in the index)"}}

    def __init__(self, session, config, parent):
        SimpleIndex.__init__(self, session, config, parent)
        self.indexingData = SimpleBitfield()
        self.indexingTerm = ""
        self.recordStore = self.get_setting(session, 'recordStore', None)
        if not self.recordStore:
            rs = self.get_path(session, 'recordStore', None)
            if rs:
                self.recordStore = rs.id
        self.resultSetClass = BitmapResultSet

    def serialise_terms(self, termid, terms, recs=0, occs=0):
        # in: list of longs
        if len(terms) == 1:
            # HACK.  Accept bitfield from mergeTerms
            bf = terms[0]
        else:
            bf = SimpleBitfield()
            for item in terms[::3]:
                bf[item] = 1
        pack = struct.pack('lll', termid, recs, occs)
        val = pack + str(bf)
        return val

    def calc_sectionOffsets(self, session, start, num, dlen):
        # order is (of course) backwards
        # so we need length etc etc.

        start = (dlen - (start / 4) +1)  - (num/4)
        packing = dlen - (start + (num/4)+1)
        return [(start, (num/4)+1, '0x', '0'*packing)]

        
    def deserialise_terms(self, data, prox=1):
	lsize = 3 * self.longStructSize
	longs = data[:lsize]
        terms = list(struct.unpack('lll', longs))
        if len(data) > lsize:
            bf = SimpleBitfield(data[lsize:])
            terms.append(bf)
        return terms

    def merge_terms(self, structTerms, newTerms, op="replace", recs=0, occs=0):
        (termid, oldTotalRecs, oldTotalOccs, oldBf) = structTerms
        if op in['add', 'replace']:
            for t in newTerms[1::3]:
                oldBf[t] = 1
        elif op == 'delete':            
            for t in newTerms[1::3]:
                oldBf[t] = 0
        trecs = oldBf.lenTrueItems()
        toccs = trecs
        merged = [termid, trecs, toccs, oldBf]
        return merged

    def construct_item(self, session, term):
        # in: single triple
        # out: resultSetItem
        # Need to map recordStore and docid at indexStore
        return self.indexStore.create_item(session, term[0], term[1], term[2])

    def construct_resultSet(self, session, terms, queryHash={}):
        # in: unpacked
        # out: resultSet
        if len(terms) > 3:
            data = terms[3]
            s = BitmapResultSet(session, data, recordStore=self.recordStore)
        else:
            bmp = SimpleBitfield(0)
            s = BitmapResultSet(session, bmp, recordStore=self.recordStore)
        s.index = self
        if queryHash:
            s.queryTerm = queryHash['text']
            s.queryFreq = queryHash['occurences']
        if (terms):
            s.termid = terms[0]
            s.totalRecs = terms[1]
            s.totalOccs = terms[2]
        else:
            s.totalRecs = 0
            s.totalOccs = 0
        return s
        

try:
    from resultSet import ArrayResultSet
except:
    raise


class RecordIdentifierIndex(Index):

    _possibleSettings = {'recordStore' : {"docs" : "The recordStore in which the records are kept (as this info not maintained in the index)"}}

    def begin_indexing(self, session):
        pass
    def commit_indexing(self, session):
        pass
    def index_record(self, session, record):
        return record
    def delete_record(self, session, record):
        pass

    def scan(self, session, clause, numReq, direction):
        raise NotImplementedError()

    def search(self, session, clause, db):
        # Copy data from clause to resultSetItem after checking exists
        recordStore = self.get_path(session, 'recordStore')
        base = SimpleResultSet(session)
        if clause.relation.value in ['=', 'exact']:
            t = clause.term.value
            if t.isdigit():                    
                 t = long(t)
            if recordStore.fetch_metadata(session, t, 'wordCount') > -1:
                item = SimpleResultSetItem(session)
                item.id = t
                item.recordStore = recordStore.id
                item.database = db.id
                items = [item]                
            else: 
                items = []
        elif clause.relation.value == 'any':
            # split on whitespace
            terms = clause.term.value.split()
            items = []
            for t in terms:
                if t.isdigit():                    
                    t = long(t)
                if recordStore.fetch_metadata(session, t, 'wordCount') > -1:
                    item = SimpleResultSetItem(session)
                    item.id = t
                    item.database = db.id
                    item.recordStore = recordStore.id
                    items.append(item)
        base.fromList(items)
        return base


# rec.checksumValue
class ReverseMetadataIndex(Index):

    _possiblePaths = {'recordStore' : {"docs" : "The recordStore in which the records are kept (as this info not maintained in the index)"}}
    _possibleSettings = {'metadataType' : {"docs" : "The type of metadata to provide an 'index' for. Defaults to digestReverse."}}

    def begin_indexing(self, session):
        pass
    def commit_indexing(self, session):
        pass
    def index_record(self, session, record):
        return record
    def delete_record(self, session, record):
        pass

    def scan(self, session, clause, numReq, direction):
        raise NotImplementedError()

    def search(self, session, clause, db):
        mtype = self.get_setting(session, 'metadataType', 'digestReverse')
 
        recordStore = self.get_path(session, 'recordStore')
        base = SimpleResultSet(session)
        if clause.relation.value in ['=', 'exact']:
            t = clause.term.value
            rid = recordStore.fetch_metadata(session, t, mtype) 
            if rid:
                item = SimpleResultSetItem(session)
                item.id = rid
                item.recordStore = recordStore.id
                item.database = db.id
                items = [item]                
            else: 
                items = []
        elif clause.relation.value == 'any':
            # split on whitespace
            terms = clause.term.value.split()
            items = []
            for t in terms:
                rid = recordStore.fetch_metadata(session, t, mtype)
                if rid:
                    item = SimpleResultSetItem(session)
                    item.id = rid
                    item.database = db.id
                    item.recordStore = recordStore.id
                    items.append(item)
        base.fromList(items)
        return base

try:
    import numarray as na
        
    class ArrayIndex(SimpleIndex):
        # Store tuples of docid, occurences only

        _possibleSettings = {'recordStore' : {"docs" : "The recordStore in which the records are kept (as this info not maintained in the index)"}}
    
        def __init__(self, session, config, parent):
            SimpleIndex.__init__(self, session, config, parent)
            self.indexStore = self.get_path(session, 'indexStore')
            self.recordStore = self.get_path(session, 'recordStore')        
            self.resultSetClass = ArrayResultSet

        def serialise_terms(self, termid, terms, recs=0, occs=0):
            # in:  list or array
            if type(terms) == types.ListType:
                if len(terms) == 1:
                    # [array()]
                    terms = terms[0]
                else:
                    # Will actually be list of triples as we haven't thrown out recStore                
                    nterms = len(terms) / 3
                    terms = na.array(terms, 'u4', shape=(nterms, 3))
                    # now throw out recStore by array mashing
                    arr2 = na.transpose(terms)
                    terms = na.transpose(
                        na.reshape(
                        na.concatenate([arr2[0], arr2[2]])
                        , (2,nterms)
                        ))
            tval = terms.tostring()
            fmt = 'lll' 
            totalRecs = len(terms)
            totalOccs = sum(terms[:,1])
            pack = struct.pack(fmt, termid, totalRecs, totalOccs)
            return pack + tval

        def deserialise_terms(self, data, prox=0):
            # in: tostring()ified array
            # w/ metadata
	    lsize = 3 * self.longStructSize
	    (termid, totalRecs, totalOccs) = struct.unpack('lll', data[:lsize])
            shape = ((len(data) -lsize) / 8, 2)        
            if shape[0]:
                return [termid, totalRecs, totalOccs, na.fromstring(data[lsize:], 'u4', shape)]
            else:
                return [termid, totalRecs, totalOccs]            

        def calc_sectionOffsets(self, session, start, num, dlen=0):
            a = (self.longStructSize * 3) + (self.longStructSize *start * 2)
            b = (self.longStructSize * 2 * num)
            return [(a,b)]

        def merge_terms(self, structTerms, newTerms, op="replace", recs=0, occs=0):
            # newTerms is a flat list
            # oldTerms is an array of pairs

            (termid, oldTotalRecs, oldTotalOccs, oldTerms) = structTerms
            
            if op == 'add':
                nterms = len(newTerms) / 3
                terms = na.array(newTerms, 'u4', shape=(nterms, 3))
                # now throw out recStore by array mashing
                arr2 = na.transpose(terms)
                occsArray = arr2[2]
                terms = na.transpose(
                    na.reshape(
                    na.concatenate([arr2[0], arr2[2]])
                    , (2,nterms)
                    ))
                merged = na.concatenate([oldTerms, terms])
                if recs != 0:
                    trecs = oldTotalRecs + recs
                    toccs = oldTotalOccs + occs
                else:
                    trecs = oldTotalRecs + nterms
                    toccs = oldTotalOccs + sum(occs)
            elif op == 'replace':
                arraydict = dict(oldTerms)
                for n in range(0,len(newTerms),3):
                    docid = newTerms[n]
                    freq = newTerms[n+2]
                    arraydict[docid] = freq
                merged = na.array(arraydict.items())
                trecs = len(arraydict)
                toccs = sum(arraydict.values())
            elif op == 'delete':            
                arraydict = dict(oldTerms)
                for n in range(0,len(newTerms),3):
                    docid = newTerms[n]
                    try:
                        del arraydict[docid]
                    except:
                        pass
                merged = na.array(arraydict.values())
                trecs = len(arraydict)
                toccs = sum(arraydict.values())
            return [termid, trecs, toccs, merged]


        def construct_resultSet(self, session, terms, queryHash={}):
            # terms should be formatted array
            if len(terms) > 2:
                rs = ArrayResultSet(session, terms[3], self.recordStore)
            else:
                rs = ArrayResultSet(session, [], self.recordStore)
            rs.index = self
            if queryHash:
                rs.queryTerm = queryHash['text']
                rs.queryFreq = queryHash['occurences']            
            if (len(terms)):
                rs.termid = terms[0]
                rs.totalRecs = terms[1]
                rs.totalOccs = terms[2]
            else:
                rs.totalRecs = 0
                rs.totalOccs = 0
            return rs


    class ProximityArrayIndex(ArrayIndex):

        _possibleSettings = {'nProxInts' : {'docs' : "", 'type' : int}}
        
        def __init__(self, session, config, parent):
            ArrayIndex.__init__(self, session, config, parent)
            self.nProxInts = self.get_setting(session, 'nProxInts', 2)

        def calc_sectionOffsets(self, session, start, num, dlen=0):
            a = (self.longStructSize * 3) + (self.longStructSize *start * 2)
            b = (self.longStructSize * self.nProxInts * num)
            return [(a,b)]
        
        def serialise_terms(self, termid, terms, recs=0, occs=0):
            # in: list of longs
            # out: LLL array L+
            flat = []
            prox = []
            t = 0
            lt = len(terms)
            if lt == 2:
                # already in right format
                a = terms[0]
                prox = terms[1]
            else:
                while t < lt:
                    # rec, store, freq, [elem, idx]+
                    (id, freq) = terms[t], terms[t+2]
                    end = t+3+(freq*self.nProxInts)
                    itemprox = terms[t+3:t+3+(freq*self.nProxInts)]
                    flat.extend([id, freq])
                    prox.extend(itemprox)                
                    t = end
                a = na.array(flat, 'u4')
            arraystr = a.tostring()            
            fmt = 'l' * (len(prox))
            params = [fmt]
            params.extend(prox)
            proxstr = struct.pack(*params)            
            head = struct.pack('lll', termid, recs, occs)
            return head + arraystr + proxstr
        
        def deserialise_terms(self, data, prox=1, numReq=0 ):
            lss = self.longStructSize * 3
            (termid, totalRecs, totalOccs) = struct.unpack('lll', data[:lss])
            if len(data) > lss:
                if numReq:
                    arrlen = numReq * 8
                    shape = (numReq, 2)
                    prox = 0
                else:
                    arrlen = totalRecs * 8
                    shape = (totalRecs, 2)        
                arr = na.fromstring(data[lss:arrlen+lss], 'u4', shape)
                
                if prox:
                    proxData = data[arrlen+lss:]
                    fmt = 'l' * (len(proxData) / 4)
                    prox = struct.unpack(fmt, proxData)            
                    # Now associate prox with item
                    itemhash = {}
                    c = 0
                    for item in arr:
                        end = c + (item[1] * self.nProxInts)
                        itemhash[item[0]] = na.array(prox[c:end], 'u4', shape=((end-c)/self.nProxInts, self.nProxInts))
                        c = end
                else:
                    itemhash = {}
                return [termid, totalRecs, totalOccs, arr, itemhash]
            else:
                return [termid, totalRecs, totalOccs]

        def merge_terms(self, structTerms, newTerms, op="replace", recs=0, occs=0):
            # newTerms is a flat list
            (termid, oldTotalRecs, oldTotalOccs, oldTerms, prox) = structTerms
            if op == 'add':
                # flatten existing and let serialise reshape
                terms = []
                for t in oldTerms:
                    terms.extend([t[0], 0, t[1]])
                    for p in prox[t[0]]:
                        terms.extend(list(p))
                terms.extend(newTerms)
                if recs != 0:
                    trecs = oldTotalRecs + recs
                    toccs = oldTotalOccs + occs
                else:
                    raise NotImplementedError                
                full = [termid, trecs, toccs]
                full.extend(terms)
                return full
            elif op == "replace":
                arraydict = dict(oldTerms)
                # now step through flat newTerms list
                lt = len(newTerms)
                t = 0
                while t < lt:
                    # rec, store, freq, [elem, idx]+
                    (id, freq) = newTerms[t], newTerms[t+2]
                    end = t+3+(freq*self.nProxInts)
                    itemprox = newTerms[t+3:t+3+(freq*self.nProxInts)]
                    arraydict[id] = freq
                    prox[id] = itemprox
                    t = end
                full = na.array(arraydict.items())
                trecs = len(arraydict)
                toccs = sum(arraydict.values())
                return [termid, trecs, toccs, full, prox]
                
            elif op == "delete":
                arraydict = dict(oldTerms)
                # now step through flat newTerms list
                lt = len(newTerms)
                t = 0
                while t < lt:
                    # rec, store, freq, [elem, idx]+
                    (id, freq) = newTerms[t], newTerms[t+2]
                    end = t+3+(freq*self.nProxInts)
                    try:
                        del arraydict[id]
                        del prox[id]
                    except KeyError:
                        print "Couldn't find %r in %r" % (id, arraydict)
                        # already deleted?
                        pass
                    t = end
                full = na.array(arraydict.items())
                trecs = len(arraydict)
                toccs = sum(arraydict.values())
                return [termid, trecs, toccs, full, prox]

        def construct_resultSet(self, session, terms, queryHash={}):
            # in: unpacked
            # out: resultSet
            if len(terms) > 2:
                rs = ArrayResultSet(session, terms[3], self.recordStore)
                rs.proxInfo = terms[4]
            else:
                rs = ArrayResultSet(session, [], self.recordStore)
            rs.index = self
            if queryHash:
                rs.queryTerm = queryHash['text']
                rs.queryFreq = queryHash['occurences']            
                rs.queryPositions = []
                # XXX also not sure about this nProxInts?
                for x in queryHash['positions'][1::self.nProxInts]:
                    rs.queryPositions.append(x)
            if (len(terms)):
                rs.termid = terms[0]
                rs.totalRecs = terms[1]
                rs.totalOccs = terms[2]
            else:
                rs.totalRecs = 0
                rs.totalOccs = 0
            return rs


except:
    raise


class ClusterExtractionIndex(SimpleIndex):

    def _handleConfigNode(self, session, node):
        if (node.localName == "cluster"):
            maps = []
            for child in node.childNodes:
                if (child.nodeType == elementType and child.localName == "map"):
                    t = child.getAttributeNS(None, 'type')
                    map = []
                    for xpchild in child.childNodes:
                        if (xpchild.nodeType == elementType and xpchild.localName == "xpath"):
                            map.append(flattenTexts(xpchild))
                        elif (xpchild.nodeType == elementType and xpchild.localName == "process"):
                            # turn xpath chain to workflow
                            ref = xpchild.getAttributeNS(None, 'ref')
                            if ref:
                                process = self.get_object(session, ref)
                            else:
                                try:
                                    xpchild.localName = 'workflow'
                                except:
                                    # 4suite dom sets read only
                                    newTop = xpchild.ownerDocument.createElementNS(None, 'workflow')
                                    for kid in xpchild.childNodes:
                                        newTop.appendChild(kid)
                                    xpchild = newTop
                                process = CachingWorkflow(session, xpchild, self)
                                process._handleConfigNode(session, xpchild)
                            map.append(process)
                    vxp = verifyXPaths([map[0]])
                    if (len(map) < 3):
                        # default ExactExtracter
                        map.append([['extracter', 'ExactExtracter']])
                    if (t == u'key'):
                        self.keyMap = [vxp[0], map[1], map[2]]
                    else:
                        maps.append([vxp[0], map[1], map[2]])
            self.maps = maps

    def __init__(self, session, config, parent):
        self.keyMap = []
        self.maps = []
        Index.__init__(self, session, config, parent)

        for m in range(len(self.maps)):
            if isinstance(self.maps[m][2], list):
                for t in range(len(self.maps[m][2])):
                    o = self.get_object(session, self.maps[m][2][t][1])
                    if (o <> None):
                        self.maps[m][2][t][1] = o
                    else:
                        raise(ConfigFileException("Unknown object %s" % (self.maps[m][2][t][1])))
        if isinstance(self.keyMap[2], list):
            for t in range(len(self.keyMap[2])):
                o = self.get_object(session, self.keyMap[2][t][1])
                if (o <> None):
                    self.keyMap[2][t][1] = o
                else:
                    raise(ConfigFileException("Unknown object %s" % (self.keyMap[2][t][1])))
            

    def begin_indexing(self, session):
        path = self.get_path(session, "tempPath")
        if (not os.path.isabs(path)):
            dfp = self.get_path(session, "defaultPath")
            path = os.path.join(dfp, path)       
        self.fileHandle = codecs.open(path, "w", 'utf-8')

    def commit_indexing(self, session):
        self.fileHandle.close()
                           

    def index_record(self, session, rec):
        # Extract cluster information, append to temp file
        # Step through .maps keys
        p = self.permissionHandlers.get('info:srw/operation/2/cluster', None)
        if p:
            if not session.user:
                raise PermissionException("Authenticated user required to cluster using %s" % self.id)
            okay = p.hasPermission(session, session.user)
            if not okay:
                raise PermissionException("Permission required to cluster using %s" % self.id)

        raw = rec.process_xpath(self.keyMap[0])
        keyData = self.keyMap[2].process(session, [raw])
        fieldData = []
        for map in self.maps:
            raw = rec.process_xpath(map[0])
            fd = map[2].process(session, [raw])
            for f in fd.keys():
                fieldData.append("%s\x00%s\x00" % (map[1], f))
        d = "".join(fieldData)
        for k in keyData.keys():
            try:
                self.fileHandle.write(u"%s\x00%s\n" % (k, d))
                self.fileHandle.flush()
            except:
                print "%s failed to write: %r" % (self.id, k)
                raise

    def delete_record(self, session, record):
        pass