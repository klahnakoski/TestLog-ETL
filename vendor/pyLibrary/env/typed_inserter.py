# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import, division, unicode_literals

from collections import Mapping

from jx_python import jx
from mo_dots import Data, ROOT_PATH, unwrap
from mo_json import NESTED, OBJECT, json2value
from mo_json.encoder import UnicodeBuilder
from mo_json.typed_encoder import typed_encode
from pyLibrary.env.elasticsearch import parse_properties, random_id


class TypedInserter(object):
    def __init__(self, es=None, id_info=None):
        self.es = es
        self.id_info = id_info
        self.get_id = jx.get(id_info.field)
        self.get_version = jx.get(id_info.version)

        if es:
            _schema = Data()
            for c in parse_properties(es.settings.alias, ".", ROOT_PATH, es.get_properties()):
                if c.es_type not in (OBJECT, NESTED):
                    _schema[c.name] = c
            self.schema = unwrap(_schema)
        else:
            self.schema = {}

    def typed_encode(self, r):
        """
        :param record:  expecting id and value properties
        :return:  dict with id and json properties
        """
        try:
            value = r['value']
            if "json" in r:
                value = json2value(r["json"])
            elif isinstance(value, Mapping) or value != None:
                pass
            else:
                from mo_logs import Log
                raise Log.error("Expecting every record given to have \"value\" or \"json\" property")

            _buffer = UnicodeBuilder(1024)
            net_new_properties = []
            path = []
            if isinstance(value, Mapping):
                given_id = self.get_id(value)
                value['_id'] = None
            else:
                given_id = None

            if given_id:
                record_id = r.get('id')
                if record_id and record_id != given_id:
                    from mo_logs import Log

                    raise Log.error(
                        "expecting {{property}} of record ({{record_id|quote}}) to match one given ({{given|quote}})",
                        property=self.id_info,
                        record_id=record_id,
                        given=given_id
                    )
            else:
                record_id = r.get('id')
                if record_id:
                    given_id = record_id
                else:
                    given_id = random_id()

            version = self.get_version(value)

            typed_encode(value, self.schema, path, net_new_properties, _buffer)
            json = _buffer.build()

            for props in net_new_properties:
                path, type = props[:-1], props[-1][1:]
                # self.es.add_column(join_field(path), type)

            return given_id, version, json
        except Exception as e:
            # THE PRETTY JSON WILL PROVIDE MORE DETAIL ABOUT THE SERIALIZATION CONCERNS
            from mo_logs import Log

            Log.error("Serialization of JSON problems", cause=e)


