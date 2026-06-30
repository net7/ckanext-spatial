"""
Some very thin wrapper classes around those in OWSLib
for convenience.
"""
import logging

from owslib.etree import etree
from owslib.fes import PropertyIsEqualTo, SortBy, SortProperty

log = logging.getLogger(__name__)

class CswError(Exception):
    pass

class OwsService(object):
    def __init__(self, endpoint=None):
        if endpoint is not None:
            self._ows(endpoint)

    def __call__(self, args):
        return getattr(self, args.operation)(**self._xmd(args))

    @classmethod
    def _operations(cls):
        return [x for x in dir(cls) if not x.startswith("_")]

    def _xmd(self, obj):
        md = {}
        for attr in [x for x in dir(obj) if not x.startswith("_")]:
            val = getattr(obj, attr)
            if not val:
                pass
            elif callable(val):
                pass
            elif isinstance(val, str):
                md[attr] = val
            elif isinstance(val, int):
                md[attr] = val
            elif isinstance(val, list):
                md[attr] = val
            else:
                md[attr] = self._xmd(val)
        return md

    def _ows(self, endpoint=None, **kw):
        if not hasattr(self, "_Implementation"):
            raise NotImplementedError("Needs an Implementation")
        if not hasattr(self, "__ows_obj__"):
            if endpoint is None:
                raise ValueError("Must specify a service endpoint")
            self.__ows_obj__ = self._Implementation(endpoint)
        return self.__ows_obj__

    def getcapabilities(self, debug=False, **kw):
        ows = self._ows(**kw)
        caps = self._xmd(ows)
        if not debug:
            if "request" in caps: del caps["request"]
            if "response" in caps: del caps["response"]
        if "owscommon" in caps: del caps["owscommon"]
        return caps

class CswService(OwsService):
    """
    Perform various operations on a CSW service
    """
    from owslib.catalogue.csw2 import CatalogueServiceWeb as _Implementation

    def __init__(self, endpoint=None, skip_caps=False):
        # net7 patch: allow skipping the GetCapabilities request/XSD validation.
        # Some CSW servers (e.g. pycsw) advertise capabilities whose XSD imports
        # remote schemas that lxml cannot resolve, which makes OWSLib raise during
        # construction. Passing skip_caps=True bypasses that validation.
        if endpoint is not None:
            self.__ows_obj__ = self._Implementation(endpoint, skip_caps=skip_caps)
        self.sortby = SortBy([SortProperty('dc:identifier')])

    def getrecords(self, qtype=None, keywords=[],
                   typenames="csw:Record", esn="brief",
                   skip=0, count=10, outputschema="gmd", **kw):
        from owslib.catalogue.csw2 import namespaces
        constraints = []
        csw = self._ows(**kw)

        if qtype is not None:
           constraints.append(PropertyIsEqualTo("dc:type", qtype))

        kwa = {
            "constraints": constraints,
            "typenames": typenames,
            "esn": esn,
            "startposition": skip,
            "maxrecords": count,
            "outputschema": namespaces[outputschema],
            "sortby": self.sortby
            }
        log.info('Making CSW request: getrecords2 %r', kwa)
        csw.getrecords2(**kwa)
        if csw.exceptionreport:
            err = 'Error getting records: %r' % \
                  csw.exceptionreport.exceptions
            #log.error(err)
            raise CswError(err)
        return [self._xmd(r) for r in list(csw.records.values())]

    def getidentifiers(self, qtype=None, typenames="csw:Record", esn="brief",
                       keywords=[], limit=None, page=10, outputschema="gmd",
                       startposition=0, cql=None, use_get=False, **kw):
        from owslib.catalogue.csw2 import namespaces
        constraints = []
        csw = self._ows(**kw)

        if qtype is not None:
           constraints.append(PropertyIsEqualTo("dc:type", qtype))

        if use_get:
            # net7 patch: gather identifiers via GET KVP requests instead of
            # getrecords2 (which uses POST). Some pycsw servers validate the POST
            # body against remote OGC XSDs and fail intermittently when those
            # schemas are unreachable ("the document is not valid ... Failed to
            # parse the XML resource ..."). GET KVP requests are not validated
            # that way and succeed reliably.
            for ident in self._getidentifiers_get(
                    typenames, esn, outputschema, page, startposition,
                    cql, limit, namespaces):
                yield ident
            return

        kwa = {
            "constraints": constraints,
            "typenames": typenames,
            "esn": esn,
            "startposition": startposition,
            "maxrecords": page,
            "outputschema": namespaces[outputschema],
            "cql": cql,
            "sortby": self.sortby
            }
        i = 0
        matches = 0
        while True:
            log.info('Making CSW request: getrecords2 %r', kwa)

            csw.getrecords2(**kwa)
            if csw.exceptionreport:
                err = 'Error getting identifiers: %r' % \
                      csw.exceptionreport.exceptions
                #log.error(err)
                raise CswError(err)

            if matches == 0:
                matches = csw.results['matches']

            identifiers = list(csw.records.keys())
            if limit is not None:
                identifiers = identifiers[:(limit-startposition)]
            for ident in identifiers:
                yield ident

            if len(identifiers) == 0:
                break

            i += len(identifiers)
            if limit is not None and i > limit:
                break

            startposition += page
            if startposition >= (matches + 1):
                break

            kwa["startposition"] = startposition

    def _getidentifiers_get(self, typenames, esn, outputschema, page,
                            startposition, cql, limit, namespaces):
        # net7 patch helper: paginate GetRecords using GET KVP requests and
        # extract the record identifiers from the response, avoiding the POST
        # path that triggers buggy remote-XSD validation on some pycsw servers.
        from owslib import util
        from owslib.util import openURL, bind_url
        from urllib.parse import urlencode

        csw = self.__ows_obj__
        GMD = 'http://www.isotc211.org/2005/gmd'
        GCO = 'http://www.isotc211.org/2005/gco'
        DC = 'http://purl.org/dc/elements/1.1/'
        CSW = 'http://www.opengis.net/cat/csw/2.0.2'
        OWS = 'http://www.opengis.net/ows'

        matches = 0
        seen = 0
        pos = startposition
        while True:
            data = {
                'service': 'CSW',
                'version': '2.0.2',
                'request': 'GetRecords',
                'typenames': typenames,
                'elementsetname': esn,
                'outputschema': namespaces[outputschema],
                'resulttype': 'results',
                # CSW startPosition is 1-based; getidentifiers uses 0-based.
                'startposition': pos + 1,
                'maxrecords': page,
            }
            if cql:
                data['constraintlanguage'] = 'CQL_TEXT'
                data['constraint'] = cql
            request = '%s%s' % (bind_url(csw.url), urlencode(data))
            log.info('Making CSW GET request: %s', request)
            response = openURL(request, None, 'Get', timeout=csw.timeout,
                               auth=csw.auth)
            # openURL returns an OWSLib ResponseWrapper whose read() takes no
            # size argument, so etree.parse() (which calls read(size)) fails.
            # Read the full body and parse it instead.
            root = etree.fromstring(response.read())

            exceptions = root.findall('.//{%s}ExceptionText' % OWS)
            if root.tag.endswith('ExceptionReport') or exceptions:
                msg = '; '.join((e.text or '').strip() for e in exceptions)
                raise CswError('Error getting identifiers (GET): %s' % (msg or 'unknown'))

            results = root.find('.//{%s}SearchResults' % CSW)
            if results is not None and matches == 0:
                matches = int(results.get('numberOfRecordsMatched', '0'))

            # Prefer ISO fileIdentifier, fall back to Dublin Core identifier.
            identifiers = [el.text for el in root.findall(
                './/{%s}fileIdentifier/{%s}CharacterString' % (GMD, GCO)) if el.text]
            if not identifiers:
                identifiers = [el.text for el in root.findall(
                    './/{%s}identifier' % DC) if el.text]

            if not identifiers:
                break

            for ident in identifiers:
                yield ident
                seen += 1
                if limit is not None and seen >= limit:
                    return

            pos += page
            if matches and pos >= matches:
                break

    def getrecordbyid(self, ids=[], esn="full", outputschema="gmd", **kw):
        from owslib.catalogue.csw2 import namespaces
        csw = self._ows(**kw)
        kwa = {
            "esn": esn,
            "outputschema": namespaces[outputschema],
            }
        # Ordinary Python version's don't support the metadata argument
        log.info('Making CSW request: getrecordbyid %r %r', ids, kwa)
        csw.getrecordbyid(ids, **kwa)
        if csw.exceptionreport:
            err = 'Error getting record by id: %r' % \
                  csw.exceptionreport.exceptions
            #log.error(err)
            raise CswError(err)
        if not csw.records:
            return
        record = self._xmd(list(csw.records.values())[0])

        ## strip off the enclosing results container, we only want the metadata
        #md = csw._exml.find("/gmd:MD_Metadata")#, namespaces=namespaces)
        # Ordinary Python version's don't support the metadata argument
        md = csw._exml.find("/{http://www.isotc211.org/2005/gmd}MD_Metadata")
        mdtree = etree.ElementTree(md)
        try:
            record["xml"] = etree.tostring(mdtree, pretty_print=True, encoding=str)
        except TypeError:
            # API incompatibilities between different flavours of elementtree
            try:
                record["xml"] = etree.tostring(mdtree, pretty_print=True, encoding=str)
            except AssertionError:
                record["xml"] = etree.tostring(md, pretty_print=True, encoding=str)

        record["xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n' + record["xml"]
        record["tree"] = mdtree
        return record
