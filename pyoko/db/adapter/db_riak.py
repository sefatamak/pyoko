# -*-  coding: utf-8 -*-
"""
this module contains a base class for other db access classes
"""

# Copyright (C) 2015 ZetaOps Inc.
#
# This file is licensed under the GNU General Public License v3
# (GPLv3).  See LICENSE.txt for details.
from collections import defaultdict

import copy

# noinspection PyCompatibility
import json
from datetime import date
import time
from datetime import datetime

from riak.util import bytes_to_str

from pyoko.db.adapter.base import BaseAdapter
from pyoko.fields import DATE_FORMAT, DATE_TIME_FORMAT

try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen

from enum import Enum
import six
from pyoko.conf import settings
from pyoko.db.connection import client, cache, log_bucket
import riak
from pyoko.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, PyokoError
import traceback
# TODO: Add OR support

import sys

ReturnType = Enum('ReturnType', 'Solr Object Model')

sys.PYOKO_STAT_COUNTER = {
    "save": 0,
    "update": 0,
    "read": 0,
    "count": 0,
    "search": 0,
}
sys.PYOKO_LOGS = defaultdict(list)

class BlockSave(object):
    def __init__(self, mdl):
        self.mdl = mdl

    def __enter__(self):
        Adapter.block_saved_keys = []
        Adapter.COLLECT_SAVES = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        indexed_obj_count = self.mdl.objects.filter(key__in=Adapter.block_saved_keys)
        while Adapter.block_saved_keys and indexed_obj_count.count() < len(Adapter.block_saved_keys):
            time.sleep(.4)
        Adapter.COLLECT_SAVES = False

# noinspection PyTypeChecker
class Adapter(BaseAdapter):
    """
    QuerySet is a lazy data access layer for Riak.
    """
    COLLECT_SAVES = False
    def __init__(self, **conf):
        super(Adapter, self).__init__(**conf)
        self.bucket = riak.RiakBucket
        self.version_bucket = riak.RiakBucket
        self._client = self._cfg.pop('client', client)
        self.index_name = ''

        if '_model_class' in conf:
            self._model_class = conf['model_class']
        if '_current_context' in conf:
            self._current_context = conf['_current_context']

        self._set_bucket(self._model_class.Meta.bucket_type,
                         self._model_class._get_bucket_name())
        # bucket datatype, eg: Dictomaping for 'map' type
        self.compiled_query = ''
        # self._solr_query = {}  # query parts, will be compiled before execution
        self._solr_query = []  # query parts, will be compiled before execution
        self._solr_params = {
            'sort': 'timestamp desc'}  # search parameters. eg: rows, fl, start, sort etc.
        self._solr_locked = False
        self._solr_cache = {}
        # self.key = None
        self._riak_cache = []  # caching riak result,
        # for repeating iterations on same query

    # ######## Development Methods  #########


    def distinct_values_of(self, field):
        # FIXME: Add support for query filters
        url = 'http://%s:8093/internal_solr/%s/select?q=-deleted%%3ATrue&wt=json&facet=true&facet.field=%s' % (
            settings.RIAK_SERVER, self.index_name, field)
        result = json.loads(bytes_to_str(urlopen(url).read()))
        dct = {}
        fresult = result['facet_counts']['facet_fields'][field]
        for i in range(0, len(fresult), 2):
            if i == len(fresult) - 1:
                break
            if fresult[i + 1]:
                dct[fresult[i]] = fresult[i + 1]
        return dct

    def _clear(self, wait=False):
        """
        clear outs the all content of current bucket
        only for development purposes
        """
        i = 0
        for k in self.bucket.get_keys():
            i += 1
            self.bucket.get(k).delete()
        if wait:
            t1 = time.time()
            while self._model_class.objects.count():
                time.sleep(0.3)
            print("\nDELETION TOOK: %s" % round(time.time() - t1, 2))
        return i

    def __iter__(self):
        self._exec_query()
        for doc in self._solr_cache['docs']:
            # if settings.DEBUG:
            #     t1 = time.time()
            obj = self.bucket.get(doc['_yz_rk'])
            if not obj.exists:
                raise ObjectDoesNotExist("Possibly a Riak <-> Solr sync delay issue!")
            yield obj.data, obj.key
            # if settings.DEBUG:
            #     sys.PYOKO_STAT_COUNTER['read'] += 1
            #     sys.PYOKO_LOGS[self._model_class.__name__].append(doc['_yz_rk'])
            #     sys._debug_db_queries.append({
            #         'TIMESTAMP': t1,
            #         'KEY': doc['_yz_rk'],
            #         'BUCKET': self.index_name,
            #         'TIME': round(time.time() - t1, 5)})

    def __deepcopy__(self, memo=None):
        """
        A deep copy method that doesn't populate caches
        and shares Riak client and bucket
        """
        obj = self.__class__(**self._cfg)
        for k, v in self.__dict__.items():
            if k.endswith(('_current_context', 'bucket', '_client', 'model_class', '_cfg')):
                obj.__dict__[k] = v
            elif k == '_riak_cache':
                obj.__dict__[k] = []
            elif k == '_solr_cache':
                obj.__dict__[k] = {}
            elif k == '_solr_query':
                obj.__dict__[k] = v[:]
            else:
                obj.__dict__[k] = copy.deepcopy(v, memo)
        obj.compiled_query = obj._pre_compiled_query or ''
        obj._solr_locked = False
        return obj

    def _set_bucket(self, type, name):
        """
        prepares bucket, sets index name
        :param str type: bucket type
        :param str name: bucket name
        :return:
        """
        if type:
            self._cfg['bucket_type'] = type
        if name:
            self._cfg['bucket_name'] = name
        self.bucket = self._client.bucket_type(self._cfg['bucket_type']
                                               ).bucket(self._cfg['bucket_name'])
        self.version_bucket = self._client.bucket_type(self._cfg['bucket_type'] + '_version'
                                                       ).bucket(self._cfg['bucket_name'] + '_log')
        self.index_name = "%s_%s" % (self._cfg['bucket_type'], self._cfg['bucket_name'])
        return self

    def _get_version_bucket(self):
        return self._client.bucket_type(self._cfg['bucket_type']
                                        ).bucket(self._cfg['bucket_name'])

    def _write_version(self, data, key, meta):
        """
            Writes a copy of the objects current state to write-once mirror bucket.

        Returns:
            Key of version record.
        """
        vdata = {'data': data,
                 'key': key,
                 'meta': meta,
                 'timestamp': time.time()}
        obj = self.version_bucket.new(data=vdata)
        obj.add_index('key_bin', key)
        obj.add_index('timestamp_int', int(vdata['timestamp']))
        obj.store()
        return obj.key

    def _write_log(self, version_key, meta_data):
        """
        Creates a log entry for current object,
        Args:
            version_key:
            meta_data:

        Returns:

        """
        meta_data = meta_data or {}
        meta_data.update({
            'key': version_key,
            'timestamp': time.time(),
        })
        if self._current_context:
            meta_data['user_id'] = self._current_context.user_id
            meta_data['role_id'] = self._current_context.role_id
        obj = log_bucket.new(data=meta_data)
        obj.add_index('key_bin', version_key)
        obj.add_index('timestamp_int', int(meta_data['timestamp']))
        obj.store()

    # def save(self, data, key=None, meta_data=None):
    #     if key is not None:
    #         obj = self.bucket.get(key)
    #         obj.data = data
    #         obj.store()
    #     else:
    #         obj = self.bucket.get(key)
    #         obj.data = data
    #         obj.store()
    #     if settings.ENABLE_VERSIONS:
    #         version_key = self._write_version(data, key, meta_data)
    #     else:
    #         version_key = ''
    #     if settings.ENABLE_ACTIVITY_LOGGING:
    #         self._write_log(version_key, meta_data)
    #     return obj.key

    def save_model(self, model, meta_data=None):
        """
        saves the model instance to riak
        :return:
        """
        # if model:
        #     self._model = model
        if settings.DEBUG:
            t1 = time.time()
        clean_value = model.clean_value()
        model._data = clean_value
        if settings.DEBUG:
            t2 = time.time()
        if not model.exist:
            obj = self.bucket.new(data=clean_value).store()
            model.key = obj.key
            new_obj = True
        else:
            new_obj = False
            obj = self.bucket.get(model.key)
            obj.data = clean_value
            obj.store()

        meta_data = meta_data or model.save_meta_data
        if settings.ENABLE_VERSIONS:
            version_key = self._write_version(clean_value, model.key, meta_data)
        else:
            version_key = ''
        if settings.ENABLE_ACTIVITY_LOGGING:
            self._write_log(version_key, meta_data)
        #
        if self.COLLECT_SAVES and new_obj:
            self.block_saved_keys.append(obj.key)
        if settings.DEBUG:
            if new_obj:
                sys.PYOKO_STAT_COUNTER['save'] += 1
                sys.PYOKO_LOGS['new'].append(obj.key)
            else:
                sys.PYOKO_LOGS[self._model_class.__name__].append(obj.key)
                sys.PYOKO_STAT_COUNTER['update'] += 1
        #     sys._debug_db_queries.append({
        #         'TIMESTAMP': t1,
        #         'KEY': obj.key,
        #         'BUCKET': self.index_name,
        #         'SAVE_IS_NEW': new_obj,
        #         'SERIALIZATION_TIME': round(t2 - t1, 5),
        #         'TIME': round(time.time() - t2, 5)
        #     })
        return model

    def get(self, key=None):
        if key:
            self._riak_cache = [self.bucket.get(key)]
        else:
            self._exec_query()
            if self.count() > 1:
                raise MultipleObjectsReturned(
                    "%s objects returned for %s" % (self.count(),
                                                    self._model_class.__name__))
        return self.get_one()

    def get_one(self):
        """
        executes solr query if needed then returns first object according to
        selected ReturnType (defaults to Model)
        :return: pyoko.Model or riak.Object or solr document
        """
        if not self._riak_cache:
            self._exec_query()
        if not self._riak_cache:
            if not self._solr_cache['docs']:
                raise ObjectDoesNotExist("%s %s" % (self.index_name, self.compiled_query))
            # if settings.DEBUG:
            #     t1 = time.time()
            self._riak_cache = [self.bucket.get(self._solr_cache['docs'][0]['_yz_rk'])]
            # if settings.DEBUG:
            # sys.PYOKO_LOGS[self._model_class.__name__].append(
            #     self._solr_cache['docs'][0]['_yz_rk'])
            # sys.PYOKO_STAT_COUNTER['read'] += 1
            # sys._debug_db_queries.append({
            #     'TIMESTAMP': t1,
            #     'KEY': self._solr_cache['docs'][0]['_yz_rk'],
            #     'BUCKET': self.index_name,
            #     'TIME': round(time.time() - t1, 5)})
        if not self._riak_cache[0].exists:
            raise ObjectDoesNotExist("%s %s" % (self.index_name,
                                                self._riak_cache[0].key))
        return self._riak_cache[0].data, self._riak_cache[0].key

    def count(self):
        """
        counts by executing solr query with rows=0 parameter
        :return:  number of objects matches to the query
        :rtype: int
        """
        if not self._solr_cache:
            self.set_params(rows=0)
            self._exec_query()
        return self._solr_cache.get('num_found', -1)

    def search_on(self, *fields, **query):
        """
        Search for query on given fields.

        Query modifier can be one of these:
            * exact
            * contains
            * startswith
            * endswith
            * range
            * lte
            * gte

        Args:
            \*fields (str): Field list to be searched on
            \*\*query:  Search query. While it's implemented as \*\*kwargs
             we only support one (first) keyword argument.

        Returns:
            Self. Queryset object.

        Examples:
            >>> Person.objects.search_on('name', 'surname', contains='john')
            >>> Person.objects.search_on('name', 'surname', startswith='jo')
        """
        search_type = list(query.keys())[0]
        parsed_query = self._parse_query_modifier(search_type, query[search_type], False)
        self.add_query([("OR_QRY", dict([(f, parsed_query) for f in fields]), True)])

    def order_by(self, *args):
        """
        Applies query ordering.

        Args:
            **args: Order by fields names.
            Defaults to ascending, prepend with hypen (-) for desecending ordering.


        """
        if self._solr_locked:
            raise Exception("Query already executed, no changes can be made."
                            "%s %s" % (self._solr_query, self._solr_params)
                            )
        self._solr_params['sort'] = ', '.join(['%s desc' % arg[1:] if arg.startswith('-')
                                               else '%s asc' % arg for arg in args])

    def set_params(self, **params):
        """
        add/update solr query parameters
        """
        if self._solr_locked:
            raise Exception("Query already executed, no changes can be made."
                            "%s %s" % (self._solr_query, self._solr_params)
                            )
        self._solr_params.update(params)

    def add_query(self, filters):
        self._solr_query.extend([f if len(f) == 3 else (f[0], f[1], False) for f in filters])

    def _escape_query(self, query, escaped=False):
        """
        Escapes query if it's not already escaped.

        Args:
            query: Query value.
            escaped (bool): expresses if query already escaped or not.

        Returns:
            Escaped query value.
        """
        if escaped:
            return query
        query = six.text_type(query)
        for e in ['+', '-', '&&', '||', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*',
                  '?', ':', ' ']:
            query = query.replace(e, "\\%s" % e)
        return query

    def _parse_query_modifier(self, modifier, qval, is_escaped):
        """
        Parses query_value according to query_type

        Args:
            modifier (str): Type of query. Exact, contains, lte etc.
            qval: Value partition of the query.

        Returns:
            Parsed query_value.
        """
        if modifier == 'range':
            if not qval[0]:
                start = '*'
            elif not is_escaped:
                start = self._escape_query(qval[0])
            else:
                start = qval[0]
            if not qval[1]:
                end = '*'
            elif not is_escaped:
                end = self._escape_query(qval[1])
            else:
                end = qval[1]
            qval = '[%s TO %s]' % (start, end)
        else:
            qval = qval if is_escaped else self._escape_query(qval)
            if modifier == 'exact':
                qval = qval
            elif modifier == 'contains':
                qval = "*%s*" % qval
            elif modifier == 'startswith':
                qval = "%s*" % qval
            elif modifier == 'endswith':
                qval = "%s*" % qval
            elif modifier == 'lte':
                qval = '[* TO %s]' % qval
            elif modifier == 'gte':
                qval = '[%s TO *]' % qval
        return qval

    def _parse_query_key(self, key, val, is_escaped):
        """
        Strips query modifier from key and call's the appropriate value modifier.

        Args:
            key (str): Query key
            val: Query value

        Returns:
            Parsed query key and value.
        """
        if key.endswith('__contains'):
            key = key[:-10]
            val = self._parse_query_modifier('contains', val, is_escaped)
        elif key.endswith('__range'):
            key = key[:-7]
            val = self._parse_query_modifier('range', val, is_escaped)
        elif key.endswith('__startswith'):
            key = key[:-12]
            val = self._parse_query_modifier('startswith', val, is_escaped)
        elif key.endswith('__endswith'):
            key = key[:-10]
            val = self._parse_query_modifier('endswith', val, is_escaped)
        # lower than or equal
        elif key.endswith('__lte'):
            key = key[:-5]
            val = self._parse_query_modifier('lte', val, is_escaped)
        # greater than or equal
        elif key.endswith('__gte'):
            key = key[:-5]
            val = self._parse_query_modifier('gte', val, is_escaped)
        return key, val

    def _compile_query(self):
        """
        Builds SOLR query and stores it into self.compiled_query
        """
        # https://wiki.apache.org/solr/SolrQuerySyntax
        # http://lucene.apache.org/core/2_9_4/queryparsersyntax.html
        query = []
        want_deleted = False
        # filtered_query = self._model_class.row_level_access(self._current_context, self)
        # if filtered_query is not None:
        #     self._solr_query += filtered_query._solr_query
        # print(self._solr_query)
        for key, val, is_escaped in self._solr_query:
            # querying on a linked model by model instance
            # it should be a Model, not a Node!
            if key == 'key':
                key = '_yz_rk'
            elif key[:5] == 'key__':  # to handle key__in etc.
                key = '_yz_rk__' + key[5:]

            if hasattr(val, '_TYPE'):
                val = val.key
                key += "_id"
                if val is None:
                    key = ('-%s' % key).replace('--', '')
                    val = '[* TO *]'
            elif isinstance(val, date):
                val = val.strftime(DATE_FORMAT)
            elif isinstance(val, datetime):
                val = val.strftime(DATE_TIME_FORMAT)
            # if it's not one of the expected objects, it should be a string
            # if key == "OR_QRY" then join them with "OR" after escaping & parsing
            elif key == 'OR_QRY':
                key = 'NOKEY'
                val = ' OR '.join(
                    ['%s:%s' % self._parse_query_key(k, v, is_escaped) for
                     k, v in val.items()])
            # __in query is same as OR_QRY but key stays same for all values
            elif key.endswith('__in'):
                key = key[:-4]
                val = ' OR '.join(
                    ['%s:%s' % (key, self._escape_query(v, is_escaped)) for v in val])
                key = 'NOKEY'
            # val is None means we're searching for empty values
            elif val is None:
                key = ('-%s' % key).replace('--', '')
                val = '[* TO *]'


            # parse the query
            key, val = self._parse_query_key(key, val, is_escaped)

            # be sure that val is properly escaped
            # val = self._escape_query(val, is_escaped)

            # as long as not explicitly asked for,
            # we filter out records with deleted flag
            if key == 'deleted':
                want_deleted = True

            # convert two underscores to dot notation
            key = key.replace('__', '.')

            # NOKEY means we already combined key partition in to "val"
            if key == 'NOKEY':
                query.append("(%s)" % val)
            else:
                query.append("%s:%s" % (key, val))

        # filter out "deleted" fields if not user explicitly asked for
        if not want_deleted:
            query.append('-deleted:True')
        # join everything with "AND"
        anded = ' AND '.join(query)
        joined_query = anded
        # if query is empty, use '*:*' instead to get anything from db.
        if joined_query == '':
            joined_query = '*:*'
        # if DEBUG is on and DEBUG_LEVEL set to a value higher than 5
        # print query in to console.
        if settings.DEBUG and settings.DEBUG_LEVEL >= 5:
            try:
                print("QRY => %s" % joined_query)
            except:
                pass

        self.compiled_query = joined_query

    def _process_params(self):
        """
        Adds default row size if it's not given in the query.
        Converts param values into unicode strings.

        Returns:
            Processed self._solr_params dict.
        """
        if 'rows' not in self._solr_params:
            self._solr_params['rows'] = self._cfg['row_size']
        for key, val in self._solr_params.items():
            if isinstance(val, str):
                self._solr_params[key] = val.encode(encoding='UTF-8')
        return self._solr_params

    def _get_debug_data(self):
        return ("                      ~=QUERY DEBUG=~                              "
                + six.text_type({
            'QUERY': self.compiled_query,
            'BUCKET': self.index_name,
            'QUERY_PARAMS': self._solr_params}))

    def _exec_query(self):
        """
        Executes solr query if it hasn't already executed.

        Returns:
            Self.
        """
        # https://github.com/basho/riak-python-client/issues/362
        # if not self._solr_cache:
        #     self.set_params(fl='_yz_rk')  # we're going to riak, fetch only keys
        if not self._solr_locked:
            if not self.compiled_query:
                self._compile_query()
            try:
                solr_params = self._process_params()
                if settings.DEBUG:
                    t1 = time.time()
                self._solr_cache = self.bucket.search(self.compiled_query,
                                                      self.index_name,
                                                      **solr_params)
                # if settings.DEBUG:
                #     sys.PYOKO_STAT_COUNTER['search'] += 1
                #     sys._debug_db_queries.append({
                #         'TIMESTAMP': t1,
                #         'QUERY': self.compiled_query,
                #         'BUCKET': self.index_name,
                #         'QUERY_PARAMS': solr_params,
                #         'TIME': round(time.time() - t1, 4)})
            except riak.RiakError as err:
                err.value += self._get_debug_data()
                raise
            self._solr_locked = True