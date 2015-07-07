# -*-  coding: utf-8 -*-
"""
this module holds classes that responsible for form generation both from models or standalone
"""

# Copyright (C) 2015 ZetaOps Inc.
#
# This file is licensed under the GNU General Public License v3
# (GPLv3).  See LICENSE.txt for details.
from pyoko.field import BaseField
from pyoko.lib.utils import un_camel, to_camel


class ModelForm(object):
    def __init__(self, model=None, **kwargs):
        """
        keyword arguments:
            base_fields = True
            nodes = True
            linked_models = True
            list_nodes = False
            types = {'field_name':'type', 'password':'password'} modify type of fields.
        :param pyoko.Model model: A pyoko model instance, may be empty or full.
        :param dict kwargs: configuration options
        """
        self.model = model or self
        if not kwargs or 'all' in kwargs:
            kwargs = {'base_fields': 1, 'nodes': 1, 'linked_models': 1}
            if 'all' in kwargs:
                kwargs['list_nodes'] = 1
        self.config = kwargs
        self.customize_types = kwargs.get('types', {})
        self.title = kwargs.get('title', self.model.__class__.__name__)

    def deserialize(self, data):
        """
        returns the model loaded with received form data.
        :param dict data: received form data from client
        """
        #FIXME: we should investigate and integrate necessary security precautions on received data
        #TODO: add listnode support when format of incoming data for listnodes defined
        proccessed_data = {}
        for key, val in data.items():
            if '.' in key:
                keys = key.split('.')
                if keys[0] not in proccessed_data:
                    proccessed_data[keys[0]] = {}
                proccessed_data[keys[0]][keys[1]] = val
            else:
                proccessed_data[key] = val
        self.model._load_data(proccessed_data)
        return self.model

    def _serialize(self):
        """
        :return: serialized model fields
        """
        # TODO: to return in consistent order we should iterate over sorted list of keys
        while 1:
            if 'base_fields' in self.config:
                for name, field in self.model._fields.items():
                    if name in ['deleted', 'timestamp']: continue
                    value = self.model._field_values.get(name, '')
                    if value:
                        default = None
                    else:
                        default = field.default() if callable(field.default) else field.default
                    yield {'name': name,
                           'type': self.customize_types.get(name, field.solr_type),
                           'value': value,
                           'required': field.required,
                           'title': field.title,
                           'default': default,
                           'section': 'main',
                           'storage': 'main',
                           }
            if 'nodes' in self.config or 'list_nodes' in self.config:
                for node_name, node in self.model._nodes.items():
                    node_type = getattr(self.model, node_name).__class__.__base__.__name__
                    if (node_type == 'Node' and 'nodes' in self.config) or (
                        node_type == 'ListNode' and 'list_nodes' in self.config):
                        instance_node = getattr(self.model, node_name)
                        for name, field in instance_node._fields.items():
                            if name in ['deleted', 'timestamp']: continue
                            yield {'name': "%s.%s" % (un_camel(node_name), name),
                                   'type': self.customize_types.get(name, field.solr_type),
                                   'title': field.title,
                                   'value': self.model._field_values.get(name, ''),
                                   'required': field.required,
                                   'default': field.default() if callable(field.default) else field.default,
                                   'section': node_name,
                                   'storage': node_type,
                                   }
            if 'linked_models' in self.config:
                for model_attr_name, model in self.model._linked_models.items():
                    yield {'name': "%s_id" % model_attr_name,
                           'model_name': model.__name__,
                           'type': 'model',
                           'title': self.model.title,
                           'value': getattr(self.model, model_attr_name).key,
                           'required': None,
                           'default': None,
                           'section': 'main',
                           }
            break



class Form(ModelForm):
    """
    base class for a custom form with pyoko.fields
    """
    def __init__(self, *args, **kwargs):
        self._nodes = {}
        self._fields = {}
        self._linked_models = {}
        self._field_values = {}
        for key, val in self.__class__.__dict__.items():
            if isinstance(val, BaseField):
                val.name = key
                self._fields[key] = val
        super(Form, self).__init__(*args, **kwargs)


    def _load_data(self, data):
        """
        fills form with data
        :param dict data:
        :return: self
        """
        for name in self._fields:
            setattr(self, name, data.get(name))
        return self