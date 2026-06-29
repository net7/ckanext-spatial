import json
import logging
import re

import shapely.geometry

try:
    from shapely.errors import GeometryTypeError
    GeometryError = (GeometryTypeError, TypeError)
except ImportError:
    # Previous version of shapely uses ValueError and TypeError
    GeometryError = (ValueError, TypeError)

from ckantoolkit import config, asbool
from ckanext.spatial.lib import normalize_bbox, fit_bbox, fit_linear_ring

log = logging.getLogger(__name__)


def wkt_to_geojson(wkt_string):
    """
    Converte una geometria WKT in formato GeoJSON
    """
    try:
        log.info(f"[WKT_CONVERSION] Input WKT: {wkt_string[:200]}...")

        # Rimuovi spazi extra e converti in maiuscolo
        wkt_string = wkt_string.strip().upper()

        # Pattern per POLYGON
        polygon_pattern = r'POLYGON\s*\(\s*\((.*?)\)\s*\)'

        match = re.match(polygon_pattern, wkt_string)
        if match:
            coords_str = match.group(1)
            log.info(f"[WKT_CONVERSION] Found POLYGON coordinates: {coords_str[:100]}...")

            # Dividi le coordinate
            coord_pairs = coords_str.split(',')
            coordinates = []

            for pair in coord_pairs:
                lon, lat = pair.strip().split()
                coordinates.append([float(lon), float(lat)])

            geojson_result = {
                "type": "Polygon",
                "coordinates": [coordinates]
            }

            log.info(f"[WKT_CONVERSION] Successfully converted to GeoJSON: {json.dumps(geojson_result)[:200]}...")
            return geojson_result

        # Se non è riconosciuto, restituisce None
        log.warning(f"[WKT_CONVERSION] WKT format not recognized: {wkt_string[:100]}...")
        return None

    except Exception as e:
        log.warning(f"[WKT_CONVERSION] Error converting WKT to GeoJSON: {str(e)}")
        return None


class SpatialSearchBackend:
    """Base class for all datastore backends."""

    def parse_geojson(self, geom_from_metadata):

        log.info(f"[SPATIAL_PARSING] Input geometry data: {str(geom_from_metadata)[:200]}...")

        try:
            geometry = json.loads(geom_from_metadata)
            log.info(f"[SPATIAL_PARSING] Successfully parsed as JSON: {json.dumps(geometry)[:200]}...")
        except (AttributeError, ValueError) as e:
            # Prova a convertire da WKT se il parsing JSON fallisce
            log.warning(
                "[SPATIAL_PARSING] Geometry not valid JSON {}, trying WKT conversion :: {}".format(
                    e, geom_from_metadata[:100]
                )
            )

            # Tenta la conversione da WKT
            geometry = wkt_to_geojson(geom_from_metadata)
            if geometry is None:
                log.error(
                    "[SPATIAL_PARSING] Geometry not valid JSON or WKT {}, not indexing :: {}".format(
                        e, geom_from_metadata[:100]
                    )
                )
                return None
            else:
                log.info("[SPATIAL_PARSING] Successfully converted WKT to GeoJSON for indexing")

        log.info(f"[SPATIAL_PARSING] Final geometry result: {json.dumps(geometry)[:200]}...")
        return geometry

    def shape_from_geometry(self, geometry):
        try:
            shape = shapely.geometry.shape(geometry)
        except GeometryError as e:
            log.error("{}, not indexing :: {}".format(e, json.dumps(geometry)[:100]))
            return None

        return shape


class SolrBBoxSearchBackend(SpatialSearchBackend):
    def index_dataset(self, dataset_dict):
        """
        We always index the envelope of the geometry regardless of
        if it's an actual bounding box (polygon)
        """

        # Controllo di sicurezza per dataset_dict
        if dataset_dict is None:
            log.error("SolrBBoxSearchBackend: dataset_dict is None")
            return {}
        
        if not isinstance(dataset_dict, dict):
            log.error("SolrBBoxSearchBackend: dataset_dict is not a dict, type: %s", type(dataset_dict))
            return {}

        try:
            geom_from_metadata = dataset_dict.get("spatial")
            if not geom_from_metadata:
                return dataset_dict

            geometry = self.parse_geojson(geom_from_metadata)
            shape = self.shape_from_geometry(geometry)

            if not shape:
                return dataset_dict

            bounds = shape.bounds

            bbox = normalize_bbox(list(bounds))
            if not bbox:
                return dataset_dict

            dataset_dict.update(bbox)

            return dataset_dict
        except Exception as e:
            log.error("SolrBBoxSearchBackend error for dataset %s: %s", dataset_dict.get('id', 'unknown'), str(e))
            return dataset_dict

    def search_params(self, bbox, search_params):
        """
        This will add the following parameters to the query:

            defType - edismax (We need to define EDisMax to use bf)
            bf - {function} A boost function to influence the score (thus
                 influencing the sorting). The algorithm can be basically defined as:

                    2 * X / Q + T

                 Where X is the intersection between the query area Q and the
                 target geometry T. It gives a ratio from 0 to 1 where 0 means
                 no overlap at all and 1 a perfect fit

             fq - Adds a filter that force the value returned by the previous
                  function to be between 0 and 1, effectively applying the
                  spatial filter.

        """

        while bbox["minx"] < -180:
            bbox["minx"] += 360
            bbox["maxx"] += 360
        while bbox["minx"] > 180:
            bbox["minx"] -= 360
            bbox["maxx"] -= 360

        values = dict(
            input_minx=bbox["minx"],
            input_maxx=bbox["maxx"],
            input_miny=bbox["miny"],
            input_maxy=bbox["maxy"],
            area_search=abs(bbox["maxx"] - bbox["minx"])
            * abs(bbox["maxy"] - bbox["miny"]),
        )

        bf = (
            """div(
                   mul(
                   mul(max(0, sub(min({input_maxx},maxx) , max({input_minx},minx))),
                       max(0, sub(min({input_maxy},maxy) , max({input_miny},miny)))
                       ),
                   2),
                   add({area_search},mul(sub(maxy, miny), sub(maxx, minx)))
                )""".format(
                **values
            )
            .replace("\n", "")
            .replace(" ", "")
        )

        search_params["fq_list"] = search_params.get("fq_list", [])
        search_params["fq_list"].append("{!frange incl=false l=0 u=1}%s" % bf)

        search_params["bf"] = bf
        search_params["defType"] = "edismax"

        return search_params


class SolrSpatialFieldSearchBackend(SpatialSearchBackend):
    def index_dataset(self, dataset_dict):
        # Controllo di sicurezza per dataset_dict
        if dataset_dict is None:
            log.error("SolrSpatialFieldSearchBackend: dataset_dict is None")
            return {}
        
        if not isinstance(dataset_dict, dict):
            log.error("SolrSpatialFieldSearchBackend: dataset_dict is not a dict, type: %s", type(dataset_dict))
            return {}

        try:
            wkt = None
            geom_from_metadata = dataset_dict.get("spatial")
            if not geom_from_metadata:
                return dataset_dict

            geometry = self.parse_geojson(geom_from_metadata)
            if not geometry:
                return dataset_dict

            # We allow multiple geometries as GeometryCollections
            if geometry["type"] == "GeometryCollection":
                geometries = geometry["geometries"]
            else:
                geometries = [geometry]

            # Check potential problems with bboxes in each geometry
            wkt = []
            for geom in geometries:
                if (
                    geom["type"] == "Polygon"
                    and len(geom["coordinates"]) == 1
                    and len(geom["coordinates"][0]) == 5
                ):

                    # Check wrong bboxes (4 same points)
                    xs = [p[0] for p in geom["coordinates"][0]]
                    ys = [p[1] for p in geom["coordinates"][0]]

                    if xs.count(xs[0]) == 5 and ys.count(ys[0]) == 5:
                        wkt.append("POINT({x} {y})".format(x=xs[0], y=ys[0]))
                    else:
                        # Check if coordinates are defined counter-clockwise,
                        # otherwise we'll get wrong results from Solr
                        lr = shapely.geometry.polygon.LinearRing(geom["coordinates"][0])
                        lr_coords = (
                            list(lr.coords)
                            if lr.is_ccw
                            else list(reversed(list(lr.coords)))
                        )
                        polygon = shapely.geometry.polygon.Polygon(
                            fit_linear_ring(lr_coords)
                        )
                        wkt.append(polygon.wkt)

            shape = self.shape_from_geometry(geometry)

            if not wkt:
                shape = shapely.geometry.shape(geometry)
                if not shape.is_valid:
                    log.error("Wrong geometry, not indexing")
                    return dataset_dict
                if shape.bounds[0] < -180 or shape.bounds[2] > 180:
                    log.error(
                        """
Geometries outside the -180, -90, 180, 90 boundaries are not supported,
you need to split the geometry in order to fit the parts. Not indexing"""
                    )
                    return dataset_dict
                wkt = shape.wkt

            dataset_dict["spatial_geom"] = wkt

            return dataset_dict
        except Exception as e:
            log.error("SolrSpatialFieldSearchBackend error for dataset %s: %s", dataset_dict.get('id', 'unknown'), str(e))
            return dataset_dict

    def search_params(self, bbox, search_params):

        bbox = fit_bbox(bbox)

        if not search_params.get("fq_list"):
            search_params["fq_list"] = []

        default_spatial_query = "{{!field f=spatial_geom}}Intersects(ENVELOPE({minx}, {maxx}, {maxy}, {miny}))"

        spatial_query = config.get("ckanext.spatial.solr_query", default_spatial_query)

        search_params["fq_list"].append(
            spatial_query.format(spatial_field="spatial_geom", **bbox)
        )

        return search_params


search_backends = {
    "solr-bbox": SolrBBoxSearchBackend,
    "solr-spatial-field": SolrSpatialFieldSearchBackend,
}
