# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import json
import sys
import os
import urllib
import logging
import re
import time
import errno
import uuid
import datetime
from bs4 import BeautifulSoup
import geoserver
import httplib2


from urlparse import urlparse
from urlparse import urlsplit
from threading import local
from collections import namedtuple
from itertools import cycle, izip
from lxml import etree
import xml.etree.ElementTree as ET
from decimal import Decimal

from owslib.wcs import WebCoverageService
from owslib.util import http_post

from django.core.exceptions import ImproperlyConfigured
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import pre_delete
from django.template.loader import render_to_string
from django.conf import settings

from dialogos.models import Comment
from agon_ratings.models import OverallRating

from gsimporter import Client
from owslib.wms import WebMapService
from geoserver.store import CoverageStore, DataStore
from geoserver.workspace import Workspace
from geoserver.catalog import Catalog
from geoserver.catalog import FailedRequestError, UploadError
from geoserver.catalog import ConflictingDataError
from geoserver.resource import FeatureType, Coverage

from geonode import GeoNodeException
from geonode.layers.utils import layer_type, get_files
from geonode.layers.models import Layer, Attribute, Style
from geonode.layers.enumerations import LAYER_ATTRIBUTE_NUMERIC_DATA_TYPES


logger = logging.getLogger(__name__)

if not hasattr(settings, 'OGC_SERVER'):
    msg = (
        'Please configure OGC_SERVER when enabling geonode.geoserver.'
        ' More info can be found at '
        'http://docs.geonode.org/en/master/reference/developers/settings.html#ogc-server')
    raise ImproperlyConfigured(msg)


def check_geoserver_is_up():
    """Verifies all geoserver is running,
       this is needed to be able to upload.
    """
    url = "%sweb/" % ogc_server_settings.LOCATION
    resp, content = http_client.request(url, "GET")
    msg = ('Cannot connect to the GeoServer at %s\nPlease make sure you '
           'have started it.' % ogc_server_settings.LOCATION)
    assert resp['status'] == '200', msg


def _add_sld_boilerplate(symbolizer):
    """
    Wrap an XML snippet representing a single symbolizer in the appropriate
    elements to make it a valid SLD which applies that symbolizer to all features,
    including format strings to allow interpolating a "name" variable in.
    """
    return """
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>%(name)s</Name>
    <UserStyle>
    <Name>%(name)s</Name>
    <Title>%(name)s</Title>
      <FeatureTypeStyle>
        <Rule>
""" + symbolizer + """
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""

_raster_template = """
<RasterSymbolizer>
    <Opacity>1.0</Opacity>
</RasterSymbolizer>
"""

_polygon_template = """
<PolygonSymbolizer>
  <Fill>
    <CssParameter name="fill">%(bg)s</CssParameter>
  </Fill>
  <Stroke>
    <CssParameter name="stroke">%(fg)s</CssParameter>
    <CssParameter name="stroke-width">0.7</CssParameter>
  </Stroke>
</PolygonSymbolizer>
"""

_line_template = """
<LineSymbolizer>
  <Stroke>
    <CssParameter name="stroke">%(bg)s</CssParameter>
    <CssParameter name="stroke-width">3</CssParameter>
  </Stroke>
</LineSymbolizer>
</Rule>
</FeatureTypeStyle>
<FeatureTypeStyle>
<Rule>
<LineSymbolizer>
  <Stroke>
    <CssParameter name="stroke">%(fg)s</CssParameter>
  </Stroke>
</LineSymbolizer>
"""

_point_template = """
<PointSymbolizer>
  <Graphic>
    <Mark>
      <WellKnownName>%(mark)s</WellKnownName>
      <Fill>
        <CssParameter name="fill">%(bg)s</CssParameter>
      </Fill>
      <Stroke>
        <CssParameter name="stroke">%(fg)s</CssParameter>
      </Stroke>
    </Mark>
    <Size>10</Size>
  </Graphic>
</PointSymbolizer>
"""

_style_templates = dict(
    raster=_add_sld_boilerplate(_raster_template),
    polygon=_add_sld_boilerplate(_polygon_template),
    line=_add_sld_boilerplate(_line_template),
    point=_add_sld_boilerplate(_point_template)
)


def _style_name(resource):
    return _punc.sub("_", resource.store.workspace.name + ":" + resource.name)


def get_sld_for(layer):
    # FIXME: GeoServer sometimes fails to associate a style with the data, so
    # for now we default to using a point style.(it works for lines and
    # polygons, hope this doesn't happen for rasters  though)
    name = layer.default_style.name if layer.default_style is not None else "point"

    # FIXME: When gsconfig.py exposes the default geometry type for vector
    # layers we should use that rather than guessing based on the auto-detected
    # style.

    if name in _style_templates:
        fg, bg, mark = _style_contexts.next()
        return _style_templates[name] % dict(
            name=layer.name,
            fg=fg,
            bg=bg,
            mark=mark)
    else:
        return None


def fixup_style(cat, resource, style):
    logger.debug("Creating styles for layers associated with [%s]", resource)
    layers = cat.get_layers(resource=resource)
    logger.info("Found %d layers associated with [%s]", len(layers), resource)
    for lyr in layers:
        if lyr.default_style.name in _style_templates:
            logger.info("%s uses a default style, generating a new one", lyr)
            name = _style_name(resource)
            if style is None:
                sld = get_sld_for(lyr)
            else:
                sld = style.read()
            logger.info("Creating style [%s]", name)
            style = cat.create_style(name, sld)
            lyr.default_style = cat.get_style(name)
            logger.info("Saving changes to %s", lyr)
            cat.save(lyr)
            logger.info("Successfully updated %s", lyr)


def cascading_delete(cat, layer_name):
    resource = None
    try:
        if layer_name.find(':') != -1:
            workspace, name = layer_name.split(':')
            ws = cat.get_workspace(workspace)
            if ws is None:
                logger.debug(
                    'cascading delete was called on a layer where the workspace was not found')
                return
            resource = cat.get_resource(name, workspace=workspace)
        else:
            resource = cat.get_resource(layer_name)
    except EnvironmentError as e:
        if e.errno == errno.ECONNREFUSED:
            msg = ('Could not connect to geoserver at "%s"'
                   'to save information for layer "%s"' % (
                       ogc_server_settings.LOCATION, layer_name)
                   )
            logger.warn(msg, e)
            return None
        else:
            raise e

    if resource is None:
            # If there is no associated resource,
            # this method can not delete anything.
            # Let's return and make a note in the log.
        logger.debug(
            'cascading_delete was called with a non existent resource')
        return
    resource_name = resource.name
    lyr = cat.get_layer(resource_name)
    if(lyr is not None):  # Already deleted
        store = resource.store
        styles = lyr.styles + [lyr.default_style]
        cat.delete(lyr)
        for s in styles:
            if s is not None and s.name not in _default_style_names:
                try:
                    cat.delete(s, purge=True)
                except FailedRequestError as e:
                    # Trying to delete a shared style will fail
                    # We'll catch the exception and log it.
                    logger.debug(e)

        # Due to a possible bug of geoserver, we need this trick for now
        try:
            cat.delete(resource)  # This will fail
        except:
            cat.reload()  # this preservers the integrity of geoserver

        if store.resource_type == 'dataStore' and 'dbtype' in store.connection_parameters and \
                store.connection_parameters['dbtype'] == 'postgis':
            delete_from_postgis(resource_name)
        elif store.type and store.type.lower() == 'geogit':
            # Prevent the entire store from being removed when the store is a
            # GeoGIT repository.
            return
        else:
            try:
                if not store.get_resources():
                    cat.delete(store, recurse=True)
            except FailedRequestError as e:
                # Catch the exception and log it.
                logger.debug(e)


def delete_from_postgis(resource_name):
    """
    Delete a table from PostGIS (because Geoserver won't do it yet);
    to be used after deleting a layer from the system.
    """
    import psycopg2
    db = ogc_server_settings.datastore_db
    conn = psycopg2.connect(
        "dbname='" +
        db['NAME'] +
        "' user='" +
        db['USER'] +
        "'  password='" +
        db['PASSWORD'] +
        "' port=" +
        db['PORT'] +
        " host='" +
        db['HOST'] +
        "'")
    try:
        cur = conn.cursor()
        cur.execute("SELECT DropGeometryTable ('%s')" % resource_name)
        conn.commit()
    except Exception as e:
        logger.error(
            "Error deleting PostGIS table %s:%s",
            resource_name,
            str(e))
    finally:
        conn.close()


def gs_slurp(
        ignore_errors=True,
        verbosity=1,
        console=None,
        owner=None,
        workspace=None,
        store=None,
        filter=None,
        skip_unadvertised=False,
        skip_geonode_registered=False,
        remove_deleted=False):
    """Configure the layers available in GeoServer in GeoNode.

       It returns a list of dictionaries with the name of the layer,
       the result of the operation and the errors and traceback if it failed.
    """
    if console is None:
        console = open(os.devnull, 'w')

    if verbosity > 1:
        print >> console, "Inspecting the available layers in GeoServer ..."
    cat = Catalog(ogc_server_settings.internal_rest, _user, _password)
    if workspace is not None:
        workspace = cat.get_workspace(workspace)
        if workspace is None:
            resources = []
        else:
            # obtain the store from within the workspace. if it exists, obtain resources
            # directly from store, otherwise return an empty list:
            if store is not None:
                store = cat.get_store(store, workspace=workspace)
                if store is None:
                    resources = []
                else:
                    resources = cat.get_resources(store=store)
            else:
                resources = cat.get_resources(workspace=workspace)

    elif store is not None:
        store = cat.get_store(store)
        resources = cat.get_resources(store=store)
    else:
        resources = cat.get_resources()
    if remove_deleted:
        resources_for_delete_compare = resources[:]
        workspace_for_delete_compare = workspace
        # filter out layers for delete comparison with GeoNode layers by following criteria:
        # enabled = true, if --skip-unadvertised: advertised = true, but
        # disregard the filter parameter in the case of deleting layers
        resources_for_delete_compare = [
            k for k in resources_for_delete_compare if k.enabled == "true"]
        if skip_unadvertised:
            resources_for_delete_compare = [
                k for k in resources_for_delete_compare if k.advertised == "true" or
                k.advertised or k.advertised is None]
    if filter:
        resources = [k for k in resources if filter in k.name]

    # filter out layers depending on enabled, advertised status:
    resources = [k for k in resources if k.enabled == "true"]
    if skip_unadvertised:
        resources = [k for k in resources if k.advertised ==
                     "true" or k.advertised or k.advertised is None]

    # filter out layers already registered in geonode
    layer_names = Layer.objects.all().values_list('typename', flat=True)
    if skip_geonode_registered:
        resources = [k for k in resources
                     if not '%s:%s' % (k.workspace.name, k.name) in layer_names]

    # TODO: Should we do something with these?
    # i.e. look for matching layers in GeoNode and also disable?
    # disabled_resources = [k for k in resources if k.enabled == "false"]

    number = len(resources)
    if verbosity > 1:
        msg = "Found %d layers, starting processing" % number
        print >> console, msg
    output = {
        'stats': {
            'failed': 0,
            'updated': 0,
            'created': 0,
            'deleted': 0,
        },
        'layers': [],
        'deleted_layers': []
    }
    start = datetime.datetime.now()
    for i, resource in enumerate(resources):
        name = resource.name
        the_store = resource.store
        workspace = the_store.workspace
        try:
            layer, created = Layer.objects.get_or_create(name=name, defaults={
                "workspace": workspace.name,
                "store": the_store.name,
                "storeType": the_store.resource_type,
                "typename": "%s:%s" % (workspace.name.encode('utf-8'), resource.name.encode('utf-8')),
                "title": resource.title or 'No title provided',
                "abstract": resource.abstract or 'No abstract provided',
                "owner": owner,
                "uuid": str(uuid.uuid4())
            })
            layer.bbox_x0 = Decimal(resource.latlon_bbox[0])
            layer.bbox_x1 = Decimal(resource.latlon_bbox[1])
            layer.bbox_y0 = Decimal(resource.latlon_bbox[2])
            layer.bbox_y1 = Decimal(resource.latlon_bbox[3])
            layer.save()
            # recalculate the layer statistics
            set_attributes(layer, overwrite=True)

        except Exception as e:
            if ignore_errors:
                status = 'failed'
                exception_type, error, traceback = sys.exc_info()
            else:
                if verbosity > 0:
                    msg = "Stopping process because --ignore-errors was not set and an error was found."
                    print >> sys.stderr, msg
                raise Exception(
                    'Failed to process %s' %
                    resource.name.encode('utf-8'), e), None, sys.exc_info()[2]
        else:
            if created:
                layer.set_default_permissions()
                status = 'created'
                output['stats']['created'] += 1
            else:
                status = 'updated'
                output['stats']['updated'] += 1

        msg = "[%s] Layer %s (%d/%d)" % (status, name, i + 1, number)
        info = {'name': name, 'status': status}
        if status == 'failed':
            output['stats']['failed'] += 1
            info['traceback'] = traceback
            info['exception_type'] = exception_type
            info['error'] = error
        output['layers'].append(info)
        if verbosity > 0:
            print >> console, msg

    if remove_deleted:
        q = Layer.objects.filter()
        if workspace_for_delete_compare is not None:
            if isinstance(workspace_for_delete_compare, Workspace):
                q = q.filter(
                    workspace__exact=workspace_for_delete_compare.name)
            else:
                q = q.filter(workspace__exact=workspace_for_delete_compare)
        if store is not None:
            if isinstance(
                    store,
                    CoverageStore) or isinstance(
                    store,
                    DataStore):
                q = q.filter(store__exact=store.name)
            else:
                q = q.filter(store__exact=store)
        logger.debug("Executing 'remove_deleted' logic")
        logger.debug("GeoNode Layers Found:")

        # compare the list of GeoNode layers obtained via query/filter with valid resources found in GeoServer
        # filtered per options passed to updatelayers: --workspace, --store, --skip-unadvertised
        # add any layers not found in GeoServer to deleted_layers (must match
        # workspace and store as well):
        deleted_layers = []
        for layer in q:
            logger.debug(
                "GeoNode Layer info: name: %s, workspace: %s, store: %s",
                layer.name,
                layer.workspace,
                layer.store)
            layer_found_in_geoserver = False
            for resource in resources_for_delete_compare:
                # if layer.name matches a GeoServer resource, check also that
                # workspace and store match, mark valid:
                if layer.name == resource.name:
                    if layer.workspace == resource.workspace.name and layer.store == resource.store.name:
                        logger.debug(
                            "Matches GeoServer layer: name: %s, workspace: %s, store: %s",
                            resource.name,
                            resource.workspace.name,
                            resource.store.name)
                        layer_found_in_geoserver = True
            if not layer_found_in_geoserver:
                logger.debug(
                    "----- Layer %s not matched, marked for deletion ---------------",
                    layer.name)
                deleted_layers.append(layer)

        number_deleted = len(deleted_layers)
        if verbosity > 1:
            msg = "\nFound %d layers to delete, starting processing" % number_deleted if number_deleted > 0 else \
                "\nFound %d layers to delete" % number_deleted
            print >> console, msg

        for i, layer in enumerate(deleted_layers):
            logger.debug(
                "GeoNode Layer to delete: name: %s, workspace: %s, store: %s",
                layer.name,
                layer.workspace,
                layer.store)
            try:
                # delete ratings, comments, and taggit tags:
                ct = ContentType.objects.get_for_model(layer)
                OverallRating.objects.filter(
                    content_type=ct,
                    object_id=layer.id).delete()
                Comment.objects.filter(
                    content_type=ct,
                    object_id=layer.id).delete()
                layer.keywords.clear()

                layer.delete()
                output['stats']['deleted'] += 1
                status = "delete_succeeded"
            except Exception as e:
                status = "delete_failed"
            finally:
                from .signals import geoserver_pre_delete
                pre_delete.connect(geoserver_pre_delete, sender=Layer)

            msg = "[%s] Layer %s (%d/%d)" % (status,
                                             layer.name,
                                             i + 1,
                                             number_deleted)
            info = {'name': layer.name, 'status': status}
            if status == "delete_failed":
                exception_type, error, traceback = sys.exc_info()
                info['traceback'] = traceback
                info['exception_type'] = exception_type
                info['error'] = error
            output['deleted_layers'].append(info)
            if verbosity > 0:
                print >> console, msg

    finish = datetime.datetime.now()
    td = finish - start
    output['stats']['duration_sec'] = td.microseconds / \
        1000000 + td.seconds + td.days * 24 * 3600
    return output


def get_stores(store_type=None):
    cat = Catalog(ogc_server_settings.internal_rest, _user, _password)
    stores = cat.get_stores()
    store_list = []
    for store in stores:
        store.fetch()
        stype = store.dom.find('type').text.lower()
        if store_type and store_type.lower() == stype:
            store_list.append({'name': store.name, 'type': stype})
        elif store_type is None:
            store_list.append({'name': store.name, 'type': stype})
    return store_list


def set_attributes(layer, overwrite=False):
    """
    Retrieve layer attribute names & types from Geoserver,
    then store in GeoNode database using Attribute model
    """
    attribute_map = []
    server_url = ogc_server_settings.LOCATION if layer.storeType != "remoteStore" else layer.service.base_url

    if layer.storeType == "remoteStore" and layer.service.ptype == "gxp_arcrestsource":
        dft_url = server_url + ("%s?f=json" % layer.typename)
        try:
            # The code below will fail if http_client cannot be imported
            body = json.loads(http_client.request(dft_url)[1])
            attribute_map = [[n["name"], _esri_types[n["type"]]]
                             for n in body["fields"] if n.get("name") and n.get("type")]
        except Exception:
            attribute_map = []

    elif layer.storeType in ["dataStore", "remoteStore", "wmsStore"]:
        dft_url = re.sub("\/wms\/?$",
                         "/",
                         server_url) + "wfs?" + urllib.urlencode({"service": "wfs",
                                                                  "version": "1.0.0",
                                                                  "request": "DescribeFeatureType",
                                                                  "typename": layer.typename.encode('utf-8'),
                                                                  })
        try:
            # The code below will fail if http_client cannot be imported  or
            # WFS not supported
            body = http_client.request(dft_url)[1]
            doc = etree.fromstring(body)
            path = ".//{xsd}extension/{xsd}sequence/{xsd}element".format(
                xsd="{http://www.w3.org/2001/XMLSchema}")

            attribute_map = [[n.attrib["name"], n.attrib["type"]] for n in doc.findall(
                path) if n.attrib.get("name") and n.attrib.get("type")]
        except Exception:
            attribute_map = []
            # Try WMS instead
            dft_url = server_url + "?" + urllib.urlencode({
                "service": "wms",
                "version": "1.0.0",
                "request": "GetFeatureInfo",
                "bbox": ','.join([str(x) for x in layer.bbox]),
                "LAYERS": layer.typename.encode('utf-8'),
                "QUERY_LAYERS": layer.typename.encode('utf-8'),
                "feature_count": 1,
                "width": 1,
                "height": 1,
                "srs": "EPSG:4326",
                "info_format": "text/html",
                "x": 1,
                "y": 1
            })
            try:
                body = http_client.request(dft_url)[1]
                soup = BeautifulSoup(body)
                for field in soup.findAll('th'):
                    if(field.string is None):
                        field_name = field.contents[0].string
                    else:
                        field_name = field.string
                    attribute_map.append([field_name, "xsd:string"])
            except Exception:
                attribute_map = []

    elif layer.storeType in ["coverageStore"]:
        dc_url = server_url + "wcs?" + urllib.urlencode({
            "service": "wcs",
            "version": "1.1.0",
            "request": "DescribeCoverage",
            "identifiers": layer.typename.encode('utf-8')
        })
        try:
            response, body = http_client.request(dc_url)
            doc = etree.fromstring(body)
            path = ".//{wcs}Axis/{wcs}AvailableKeys/{wcs}Key".format(
                wcs="{http://www.opengis.net/wcs/1.1.1}")
            attribute_map = [[n.text, "raster"] for n in doc.findall(path)]
        except Exception:
            attribute_map = []

    attributes = layer.attribute_set.all()
    # Delete existing attributes if they no longer exist in an updated layer
    for la in attributes:
        lafound = False
        for field, ftype in attribute_map:
            if field == la.attribute:
                lafound = True
        if overwrite or not lafound:
            logger.debug(
                "Going to delete [%s] for [%s]",
                la.attribute,
                layer.name.encode('utf-8'))
            la.delete()

    # Add new layer attributes if they don't already exist
    if attribute_map is not None:
        iter = len(Attribute.objects.filter(layer=layer)) + 1
        for field, ftype in attribute_map:
            if field is not None:
                la, created = Attribute.objects.get_or_create(
                    layer=layer, attribute=field, attribute_type=ftype)
                if created:
                    if is_layer_attribute_aggregable(
                            layer.storeType,
                            field,
                            ftype):
                        logger.debug("Generating layer attribute statistics")
                        result = get_attribute_statistics(layer.name, field)
                        if result is not None:
                            la.count = result['Count']
                            la.min = result['Min']
                            la.max = result['Max']
                            la.average = result['Average']
                            la.median = result['Median']
                            la.stddev = result['StandardDeviation']
                            la.sum = result['Sum']
                            la.unique_values = result['unique_values']
                            la.last_stats_updated = datetime.datetime.now()
                    la.attribute_label = field.title()
                    la.visible = ftype.find("gml:") != 0
                    la.display_order = iter
                    la.save()
                    iter += 1
                    logger.debug(
                        "Created [%s] attribute for [%s]",
                        field,
                        layer.name.encode('utf-8'))
    else:
        logger.debug("No attributes found")


def set_styles(layer, gs_catalog):
    style_set = []
    gs_layer = gs_catalog.get_layer(layer.name)
    default_style = gs_layer.default_style
    layer.default_style = save_style(default_style)
    style_set.append(layer.default_style)

    alt_styles = gs_layer.styles

    for alt_style in alt_styles:
        style_set.append(save_style(alt_style))

    layer.styles = style_set
    return layer


def save_style(gs_style):
    style, created = Style.objects.get_or_create(name=gs_style.sld_name)
    style.sld_title = gs_style.sld_title
    style.sld_body = gs_style.sld_body
    style.sld_url = gs_style.body_href()
    style.save()
    return style


def is_layer_attribute_aggregable(store_type, field_name, field_type):
    """
    Decipher whether layer attribute is suitable for statistical derivation
    """

    # must be vector layer
    if store_type != 'dataStore':
        return False
    # must be a numeric data type
    if field_type not in LAYER_ATTRIBUTE_NUMERIC_DATA_TYPES:
        return False
    # must not be an identifier type field
    if field_name.lower() in ['id', 'identifier']:
        return False

    return True


def get_attribute_statistics(layer_name, field):
    """
    Generate statistics (range, mean, median, standard deviation, unique values)
    for layer attribute
    """

    logger.debug('Deriving aggregate statistics for attribute %s', field)

    if not ogc_server_settings.WPS_ENABLED:
        return None
    try:
        return wps_execute_layer_attribute_statistics(layer_name, field)
    except Exception:
        logger.exception('Error generating layer aggregate statistics')


def get_wcs_record(instance, retry=True):
    wcs = WebCoverageService(ogc_server_settings.public_url + 'wcs', '1.0.0')
    key = instance.workspace + ':' + instance.name
    if key in wcs.contents:
        return wcs.contents[key]
    else:
        msg = ("Layer '%s' was not found in WCS service at %s." %
               (key, ogc_server_settings.public_url)
               )
        if retry:
            logger.debug(
                msg +
                ' Waiting a couple of seconds before trying again.')
            time.sleep(2)
            return get_wcs_record(instance, retry=False)
        else:
            raise GeoNodeException(msg)


def get_coverage_grid_extent(instance):
    """
        Returns a list of integers with the size of the coverage
        extent in pixels
    """
    instance_wcs = get_wcs_record(instance)
    grid = instance_wcs.grid
    return [(int(h) - int(l) + 1) for
            h, l in zip(grid.highlimits, grid.lowlimits)]


GEOSERVER_LAYER_TYPES = {
    'vector': FeatureType.resource_type,
    'raster': Coverage.resource_type,
}


def geoserver_layer_type(filename):
    the_type = layer_type(filename)
    return GEOSERVER_LAYER_TYPES[the_type]


def cleanup(name, uuid):
    """Deletes GeoServer and Catalogue records for a given name.

       Useful to clean the mess when something goes terribly wrong.
       It also verifies if the Django record existed, in which case
       it performs no action.
    """
    try:
        Layer.objects.get(name=name)
    except Layer.DoesNotExist as e:
        pass
    else:
        msg = ('Not doing any cleanup because the layer %s exists in the '
               'Django db.' % name)
        raise GeoNodeException(msg)

    cat = gs_catalog
    gs_store = None
    gs_layer = None
    gs_resource = None
    # FIXME: Could this lead to someone deleting for example a postgis db
    # with the same name of the uploaded file?.
    try:
        gs_store = cat.get_store(name)
        if gs_store is not None:
            gs_layer = cat.get_layer(name)
            if gs_layer is not None:
                gs_resource = gs_layer.resource
        else:
            gs_layer = None
            gs_resource = None
    except FailedRequestError as e:
        msg = ('Couldn\'t connect to GeoServer while cleaning up layer '
               '[%s] !!', str(e))
        logger.warning(msg)

    if gs_layer is not None:
        try:
            cat.delete(gs_layer)
        except:
            logger.warning("Couldn't delete GeoServer layer during cleanup()")
    if gs_resource is not None:
        try:
            cat.delete(gs_resource)
        except:
            msg = 'Couldn\'t delete GeoServer resource during cleanup()'
            logger.warning(msg)
    if gs_store is not None:
        try:
            cat.delete(gs_store)
        except:
            logger.warning("Couldn't delete GeoServer store during cleanup()")

    logger.warning('Deleting dangling Catalogue record for [%s] '
                   '(no Django record to match)', name)

    if 'geonode.catalogue' in settings.INSTALLED_APPS:
        from geonode.catalogue import get_catalogue
        catalogue = get_catalogue()
        catalogue.remove_record(uuid)
        logger.warning('Finished cleanup after failed Catalogue/Django '
                       'import for layer: %s', name)


def _create_featurestore(name, data, overwrite=False, charset="UTF-8"):
    cat = gs_catalog
    cat.create_featurestore(name, data, overwrite=overwrite, charset=charset)
    return cat.get_store(name), cat.get_resource(name)


def _create_coveragestore(name, data, overwrite=False, charset="UTF-8"):
    cat = gs_catalog
    cat.create_coveragestore(name, data, overwrite=overwrite)
    return cat.get_store(name), cat.get_resource(name)


def _create_db_featurestore(name, data, overwrite=False, charset="UTF-8"):
    """Create a database store then use it to import a shapefile.

    If the import into the database fails then delete the store
    (and delete the PostGIS table for it).
    """
    cat = gs_catalog
    dsname = ogc_server_settings.DATASTORE

    try:
        ds = cat.get_store(dsname)
    except FailedRequestError:
        ds = cat.create_datastore(dsname)
        db = ogc_server_settings.datastore_db
        db_engine = 'postgis' if \
            'postgis' in db['ENGINE'] else db['ENGINE']
        ds.connection_parameters.update(
            host=db['HOST'],
            port=db['PORT'],
            database=db['NAME'],
            user=db['USER'],
            passwd=db['PASSWORD'],
            dbtype=db_engine
        )
        cat.save(ds)
        ds = cat.get_store(dsname)

    try:
        cat.add_data_to_store(ds, name, data,
                              overwrite=overwrite,
                              charset=charset)
        return ds, cat.get_resource(name, store=ds)
    except Exception:
        # FIXME(Ariel): This is not a good idea, today there was a problem
        # accessing postgis that caused add_data_to_store to fail,
        # for the same reasons the call to delete_from_postgis below failed too
        # I am commenting it out and filing it as issue #1058
        # delete_from_postgis(name)
        raise


def geoserver_upload(
        layer,
        base_file,
        user,
        name,
        overwrite=True,
        title=None,
        abstract=None,
        permissions=None,
        keywords=(),
        charset='UTF-8'):

    # Step 2. Check that it is uploading to the same resource type as
    # the existing resource
    logger.info('>>> Step 2. Make sure we are not trying to overwrite a '
                'existing resource named [%s] with the wrong type', name)
    the_layer_type = geoserver_layer_type(base_file)

    # Get a short handle to the gsconfig geoserver catalog
    cat = gs_catalog

    # Check if the store exists in geoserver
    try:
        store = cat.get_store(name)
    except geoserver.catalog.FailedRequestError as e:
        # There is no store, ergo the road is clear
        pass
    else:
        # If we get a store, we do the following:
        resources = store.get_resources()

        # If the store is empty, we just delete it.
        if len(resources) == 0:
            cat.delete(store)
        else:
            # If our resource is already configured in the store it needs
            # to have the right resource type
            for resource in resources:
                if resource.name == name:
                    msg = 'Name already in use and overwrite is False'
                    assert overwrite, msg
                    existing_type = resource.resource_type
                    if existing_type != the_layer_type:
                        msg = ('Type of uploaded file %s (%s) '
                               'does not match type of existing '
                               'resource type '
                               '%s' % (name, the_layer_type, existing_type))
                        logger.info(msg)
                        raise GeoNodeException(msg)

    # Step 3. Identify whether it is vector or raster and which extra files
    # are needed.
    logger.info('>>> Step 3. Identifying if [%s] is vector or raster and '
                'gathering extra files', name)
    if the_layer_type == FeatureType.resource_type:
        logger.debug('Uploading vector layer: [%s]', base_file)
        if ogc_server_settings.DATASTORE:
            create_store_and_resource = _create_db_featurestore
        else:
            create_store_and_resource = _create_featurestore
    elif the_layer_type == Coverage.resource_type:
        logger.debug("Uploading raster layer: [%s]", base_file)
        create_store_and_resource = _create_coveragestore
    else:
        msg = ('The layer type for name %s is %s. It should be '
               '%s or %s,' % (name,
                              the_layer_type,
                              FeatureType.resource_type,
                              Coverage.resource_type))
        logger.warn(msg)
        raise GeoNodeException(msg)

    # Step 4. Create the store in GeoServer
    logger.info('>>> Step 4. Starting upload of [%s] to GeoServer...', name)

    # Get the helper files if they exist
    files = get_files(base_file)

    data = files

    if 'shp' not in files:
        data = base_file

    try:
        store, gs_resource = create_store_and_resource(name,
                                                       data,
                                                       charset=charset,
                                                       overwrite=overwrite)
    except UploadError as e:
        msg = ('Could not save the layer %s, there was an upload '
               'error: %s' % (name, str(e)))
        logger.warn(msg)
        e.args = (msg,)
        raise
    except ConflictingDataError as e:
        # A datastore of this name already exists
        msg = ('GeoServer reported a conflict creating a store with name %s: '
               '"%s". This should never happen because a brand new name '
               'should have been generated. But since it happened, '
               'try renaming the file or deleting the store in '
               'GeoServer.' % (name, str(e)))
        logger.warn(msg)
        e.args = (msg,)
        raise
    else:
        logger.debug('Finished upload of [%s] to GeoServer without '
                     'errors.', name)

    # Step 5. Create the resource in GeoServer
    logger.info('>>> Step 5. Generating the metadata for [%s] after '
                'successful import to GeoSever', name)

    # Verify the resource was created
    if gs_resource is not None:
        assert gs_resource.name == name
    else:
        msg = ('GeoNode encountered problems when creating layer %s.'
               'It cannot find the Layer that matches this Workspace.'
               'try renaming your files.' % name)
        logger.warn(msg)
        raise GeoNodeException(msg)

    # Step 6. Make sure our data always has a valid projection
    # FIXME: Put this in gsconfig.py
    logger.info('>>> Step 6. Making sure [%s] has a valid projection' % name)
    if gs_resource.latlon_bbox is None:
        box = gs_resource.native_bbox[:4]
        minx, maxx, miny, maxy = [float(a) for a in box]
        if -180 <= minx <= 180 and -180 <= maxx <= 180 and \
           -90 <= miny <= 90 and -90 <= maxy <= 90:
            logger.info('GeoServer failed to detect the projection for layer '
                        '[%s]. Guessing EPSG:4326', name)
            # If GeoServer couldn't figure out the projection, we just
            # assume it's lat/lon to avoid a bad GeoServer configuration

            gs_resource.latlon_bbox = gs_resource.native_bbox
            gs_resource.projection = "EPSG:4326"
            cat.save(gs_resource)
        else:
            msg = ('GeoServer failed to detect the projection for layer '
                   '[%s]. It doesn\'t look like EPSG:4326, so backing out '
                   'the layer.')
            logger.info(msg, name)
            cascading_delete(cat, name)
            raise GeoNodeException(msg % name)

    # Step 7. Create the style and assign it to the created resource
    # FIXME: Put this in gsconfig.py
    logger.info('>>> Step 7. Creating style for [%s]' % name)
    publishing = cat.get_layer(name)

    if 'sld' in files:
        f = open(files['sld'], 'r')
        sld = f.read()
        f.close()
    else:
        sld = get_sld_for(publishing)

    if sld is not None:
        try:
            cat.create_style(name, sld)
        except geoserver.catalog.ConflictingDataError as e:
            msg = ('There was already a style named %s in GeoServer, '
                   'cannot overwrite: "%s"' % (name, str(e)))
            logger.warn(msg)
            e.args = (msg,)

        # FIXME: Should we use the fully qualified typename?
        publishing.default_style = cat.get_style(name)
        cat.save(publishing)

    # Step 10. Create the Django record for the layer
    logger.info('>>> Step 10. Creating Django record for [%s]', name)
    # FIXME: Do this inside the layer object
    typename = gs_resource.store.workspace.name + ':' + gs_resource.name
    layer_uuid = str(uuid.uuid1())
    defaults = dict(store=gs_resource.store.name,
                    storeType=gs_resource.store.resource_type,
                    typename=typename,
                    title=title or gs_resource.title,
                    uuid=layer_uuid,
                    abstract=abstract or gs_resource.abstract or '',
                    owner=user)

    workspace = gs_resource.store.workspace.name

    return name, workspace, defaults


class ServerDoesNotExist(Exception):
    pass


class OGC_Server(object):

    """
    OGC Server object.
    """

    def __init__(self, ogc_server, alias):
        self.alias = alias
        self.server = ogc_server

    def __getattr__(self, item):
        return self.server.get(item)

    @property
    def credentials(self):
        """
        Returns a tuple of the server's credentials.
        """
        creds = namedtuple('OGC_SERVER_CREDENTIALS', ['username', 'password'])
        return creds(username=self.USER, password=self.PASSWORD)

    @property
    def datastore_db(self):
        """
        Returns the server's datastore dict or None.
        """
        if self.DATASTORE and settings.DATABASES.get(self.DATASTORE, None):
            return settings.DATABASES.get(self.DATASTORE, dict())
        else:
            return dict()

    @property
    def ows(self):
        """
        The Open Web Service url for the server.
        """
        location = self.PUBLIC_LOCATION if self.PUBLIC_LOCATION else self.LOCATION
        return self.OWS_LOCATION if self.OWS_LOCATION else location + 'ows'

    @property
    def rest(self):
        """
        The REST endpoint for the server.
        """
        return self.LOCATION + \
            'rest' if not self.REST_LOCATION else self.REST_LOCATION

    @property
    def public_url(self):
        """
        The global public endpoint for the server.
        """
        return self.LOCATION if not self.PUBLIC_LOCATION else self.PUBLIC_LOCATION

    @property
    def internal_ows(self):
        """
        The Open Web Service url for the server used by GeoNode internally.
        """
        location = self.LOCATION
        return location + 'ows'

    @property
    def internal_rest(self):
        """
        The internal REST endpoint for the server.
        """
        return self.LOCATION + 'rest'

    @property
    def hostname(self):
        return urlsplit(self.LOCATION).hostname

    @property
    def netloc(self):
        return urlsplit(self.LOCATION).netloc

    def __str__(self):
        return self.alias


class OGC_Servers_Handler(object):

    """
    OGC Server Settings Convenience dict.
    """

    def __init__(self, ogc_server_dict):
        self.servers = ogc_server_dict
        # FIXME(Ariel): Are there better ways to do this without involving
        # local?
        self._servers = local()

    def ensure_valid_configuration(self, alias):
        """
        Ensures the settings are valid.
        """
        try:
            server = self.servers[alias]
        except KeyError:
            raise ServerDoesNotExist("The server %s doesn't exist" % alias)

        datastore = server.get('DATASTORE')
        uploader_backend = getattr(
            settings,
            'UPLOADER',
            dict()).get(
            'BACKEND',
            'geonode.rest')

        if uploader_backend == 'geonode.importer' and datastore and not settings.DATABASES.get(
                datastore):
            raise ImproperlyConfigured(
                'The OGC_SERVER setting specifies a datastore '
                'but no connection parameters are present.')

        if uploader_backend == 'geonode.importer' and not datastore:
            raise ImproperlyConfigured(
                'The UPLOADER BACKEND is set to geonode.importer but no DATASTORE is specified.')

        if 'PRINTNG_ENABLED' in server:
            raise ImproperlyConfigured("The PRINTNG_ENABLED setting has been removed, use 'PRINT_NG_ENABLED' instead.")

    def ensure_defaults(self, alias):
        """
        Puts the defaults into the settings dictionary for a given connection where no settings is provided.
        """
        try:
            server = self.servers[alias]
        except KeyError:
            raise ServerDoesNotExist("The server %s doesn't exist" % alias)

        server.setdefault('BACKEND', 'geonode.geoserver')
        server.setdefault('LOCATION', 'http://localhost:8080/geoserver/')
        server.setdefault('USER', 'admin')
        server.setdefault('PASSWORD', 'geoserver')
        server.setdefault('DATASTORE', str())
        server.setdefault('GEOGIT_DATASTORE_DIR', str())

        for option in ['MAPFISH_PRINT_ENABLED', 'PRINT_NG_ENABLED', 'GEONODE_SECURITY_ENABLED',
                       'BACKEND_WRITE_ENABLED']:
            server.setdefault(option, True)

        for option in ['GEOGIT_ENABLED', 'WMST_ENABLED', 'WPS_ENABLED']:
            server.setdefault(option, False)

    def __getitem__(self, alias):
        if hasattr(self._servers, alias):
            return getattr(self._servers, alias)

        self.ensure_defaults(alias)
        self.ensure_valid_configuration(alias)
        server = self.servers[alias]
        server = OGC_Server(alias=alias, ogc_server=server)
        setattr(self._servers, alias, server)
        return server

    def __setitem__(self, key, value):
        setattr(self._servers, key, value)

    def __iter__(self):
        return iter(self.servers)

    def all(self):
        return [self[alias] for alias in self]


def get_wms():
    wms_url = ogc_server_settings.internal_ows + \
        "?service=WMS&request=GetCapabilities&version=1.1.0"
    netloc = urlparse(wms_url).netloc
    http = httplib2.Http()
    http.add_credentials(_user, _password)
    http.authorizations.append(
        httplib2.BasicAuthentication(
            (_user, _password),
            netloc,
            wms_url,
            {},
            None,
            None,
            http
        )
    )
    body = http.request(wms_url)[1]
    _wms = WebMapService(wms_url, xml=body)
    return _wms


def wps_execute_layer_attribute_statistics(layer_name, field):
    """Derive aggregate statistics from WPS endpoint"""

    # generate statistics using WPS
    url = '%s/ows' % (ogc_server_settings.LOCATION)

    # TODO: use owslib.wps.WebProcessingService for WPS interaction
    # this requires GeoServer's WPS gs:Aggregate function to
    # return a proper wps:ExecuteResponse

    request = render_to_string('layers/wps_execute_gs_aggregate.xml', {
                               'layer_name': 'geonode:%s' % layer_name,
                               'field': field
                               })

    response = http_post(url, request, timeout=ogc_server_settings.TIMEOUT)

    exml = etree.fromstring(response)

    result = {}

    for f in ['Min', 'Max', 'Average', 'Median', 'StandardDeviation', 'Sum']:
        fr = exml.find(f)
        if fr is not None:
            result[f] = fr.text
        else:
            result[f] = 'NA'

    count = exml.find('Count')
    if count is not None:
        result['Count'] = int(count.text)
    else:
        result['Count'] = 0

    result['unique_values'] = 'NA'

    # TODO: find way of figuring out threshold better
    if result['Count'] < 10000:
        request = render_to_string('layers/wps_execute_gs_unique.xml', {
                                   'layer_name': 'geonode:%s' % layer_name,
                                   'field': field
                                   })

        response = http_post(url, request, timeout=ogc_server_settings.TIMEOUT)

        exml = etree.fromstring(response)


def style_update(request, url):
    """
    Sync style stuff from GS to GN.
    Ideally we should call this from a view straight from GXP, and we should use
    gsConfig, that at this time does not support styles updates. Before gsConfig
    is updated, for now we need to parse xml.
    In case of a DELETE, we need to query request.path to get the style name,
    and then remove it.
    In case of a POST or PUT, we need to parse the xml from
    request.body, which is in this format:
    """
    if request.method in ('POST', 'PUT'):  # we need to parse xml
        tree = ET.ElementTree(ET.fromstring(request.body))
        elm_namedlayer_name = tree.findall(
            './/{http://www.opengis.net/sld}Name')[0]
        elm_user_style_name = tree.findall(
            './/{http://www.opengis.net/sld}Name')[1]
        elm_user_style_title = tree.find(
            './/{http://www.opengis.net/sld}Title')
        if not elm_user_style_title:
            elm_user_style_title = elm_user_style_name
        layer_name = elm_namedlayer_name.text
        style_name = elm_user_style_name.text
        sld_body = '<?xml version="1.0" encoding="UTF-8"?>%s' % request.body
        # add style in GN and associate it to layer
        if request.method == 'POST':
            style = Style(name=style_name, sld_body=sld_body, sld_url=url)
            style.save()
            layer = Layer.objects.all().filter(typename=layer_name)[0]
            style.layer_styles.add(layer)
            style.save()
        if request.method == 'PUT':  # update style in GN
            style = Style.objects.all().filter(name=style_name)[0]
            style.sld_body = sld_body
            style.sld_url = url
            if len(elm_user_style_title.text) > 0:
                style.sld_title = elm_user_style_title.text
            style.save()
            for layer in style.layer_styles.all():
                layer.save()
    if request.method == 'DELETE':  # delete style from GN
        style_name = os.path.basename(request.path)
        style = Style.objects.all().filter(name=style_name)[0]
        style.delete()

ogc_server_settings = OGC_Servers_Handler(settings.OGC_SERVER)['default']

_wms = None
_csw = None
_user, _password = ogc_server_settings.credentials

http_client = httplib2.Http()
http_client.add_credentials(_user, _password)
http_client.add_credentials(_user, _password)
_netloc = urlparse(ogc_server_settings.LOCATION).netloc
http_client.authorizations.append(
    httplib2.BasicAuthentication(
        (_user, _password),
        _netloc,
        ogc_server_settings.LOCATION,
        {},
        None,
        None,
        http_client
    )
)


url = ogc_server_settings.rest
gs_catalog = Catalog(url, _user, _password)
gs_uploader = Client(url, _user, _password)

_punc = re.compile(r"[\.:]")  # regex for punctuation that confuses restconfig
_foregrounds = [
    "#ffbbbb",
    "#bbffbb",
    "#bbbbff",
    "#ffffbb",
    "#bbffff",
    "#ffbbff"]
_backgrounds = [
    "#880000",
    "#008800",
    "#000088",
    "#888800",
    "#008888",
    "#880088"]
_marks = ["square", "circle", "cross", "x", "triangle"]
_style_contexts = izip(cycle(_foregrounds), cycle(_backgrounds), cycle(_marks))
_default_style_names = ["point", "line", "polygon", "raster"]
_esri_types = {
    "esriFieldTypeDouble": "xsd:double",
    "esriFieldTypeString": "xsd:string",
    "esriFieldTypeSmallInteger": "xsd:int",
    "esriFieldTypeInteger": "xsd:int",
    "esriFieldTypeDate": "xsd:dateTime",
    "esriFieldTypeOID": "xsd:long",
    "esriFieldTypeGeometry": "xsd:geometry",
    "esriFieldTypeBlob": "xsd:base64Binary",
    "esriFieldTypeRaster": "raster",
    "esriFieldTypeGUID": "xsd:string",
    "esriFieldTypeGlobalID": "xsd:string",
    "esriFieldTypeXML": "xsd:anyType"}
