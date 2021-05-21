import geopandas as gpd
from pathlib import Path
import numpy as np
import pandas as pd
from shapely import wkt
from shapely.geometry import Point
from scipy.spatial import cKDTree
import geopy.distance
import geocoder
import time
import math
from typing import List, Optional


class RawSolarDatabase:
    def from_csv(self, file_path: Path):

        # Load PV database file and convert it to a Geopandas.GeoDataFrame with EPSG:4326 as the coordinate reference system
        solar_db = pd.read_csv(
            file_path,
            sep=";",
            header=None,
            names=["Current_Tile_240", "UL_Image_16", "geometry"],
        )

        solar_db["geometry"] = solar_db["geometry"].apply(wkt.loads)

        solar_db = gpd.GeoDataFrame(solar_db, geometry="geometry")
        solar_db.crs = {"init": "epsg:4326"}
        solar_db["class"] = int(1)
        return solar_db[["class", "geometry"]]


class RegistryCreator:
    """
    Creates an address-level PV registry for the specified county by bringing together the information obtained from the tile processing step with the county's 3D rooftop data.

    county : str
        Name of the county for which the enrichted automated PV registry is created.
    PV_gdf : GeoDataFrame
        Contains all the identified and segmented PV panels within a given county based on the results from the previous tile processing step.
    rooftop_gdf : GeoDataFrame
        Contains all the rooftop information such as a rooftop's tilt, its azimuth, and its geo-referenced polygon derived from openNRW's 3D building data.
    bing_key : str
        Your Bing API key which is needed to reverse geocode lat, lon values into actual street addresses.
    """

    def __init__(self, configuration):
        """
        Parameters
        ----------
        configuration : dict
            The configuration based on config.yml in dict format.
        """

        self.county = configuration.get("county4analysis")
        # replace with f"/data/pv_database/{self.county}_PV_db.csv"
        self.raw_PV_polygons_gdf = RawSolarDatabase().from_csv(
            Path(
                f"/Users/kevin/Projects/Active/PV4GERFiles/pv4ger/data/pv_database/{self.county}_PV_db.csv"
            )
        )

        # replacde with Path(f"{configuration['rooftop_data_dir']}/{self.county}_Clipped.geojson")
        self.rooftop_gdf = gpd.read_file(
            Path(
                f"/Users/kevin/Projects/Active/PV4GERFiles/pv4ger/{configuration['rooftop_data_dir']}"
                f"/{self.county}_Clipped.geojson"
            )
        )
        self.rooftop_gdf.crs = {"init": "epsg:4326"}

        self.bing_key = configuration["bing_key"]

    def aggregate_raw_PV_polygons_to_raw_PV_installations(self):
        """
        Aggregate raw PV polygons belonging to the same PV installation. Raw refers to the fact that the PV area is
        not corrected by the tilt angle.
        For each PV installation, we compute its raw area and a unique identifier.
        """

        # Buffer polygons, i.e. overwrite the original polygons with their buffered versions
        # Based on our experience, the buffer value should be within [1e-6, 1e-8] degrees
        self.raw_PV_polygons_gdf["geometry"] = self.raw_PV_polygons_gdf[
            "geometry"
        ].buffer(1e-6)

        # Dissolve, i.e. aggregate, all PV polygons into one Multipolygon
        self.raw_PV_polygons_gdf = self.raw_PV_polygons_gdf.dissolve(by="class")

        # Explode multi-part geometries into multiple single geometries
        self.raw_PV_installations_gdf = (
            self.raw_PV_polygons_gdf.explode().reset_index().drop(columns=["level_1"])
        )

        # Compute the raw area for each pv installation
        self.raw_PV_installations_gdf["raw_area"] = (
            self.raw_PV_installations_gdf["geometry"].to_crs(epsg=5243).area
        )

        # Create a unique identifier for each pv installation
        self.raw_PV_installations_gdf[
            "identifier"
        ] = self.raw_PV_installations_gdf.index.map(lambda id: "polygon_" + str(id))

    def overlay_raw_PV_installations_and_rooftops(self):

        # Intersect PV panels and rooftop polygons to enrich all the PV polygons with the attributes of their respective rooftop polygon
        self.raw_PV_installations_on_rooftop = gpd.overlay(
            self.raw_PV_installations_gdf, self.rooftop_gdf, how="intersection"
        )

        self.raw_PV_installations_on_rooftop["area_inter"] = (
            self.raw_PV_installations_on_rooftop["geometry"].to_crs(epsg=5243).area
        )

        # PV polygons which are not on rooftops. This includes free-standing PV units and geometries overhanging from rooftops
        self.raw_PV_installations_off_rooftop = gpd.overlay(
            self.raw_PV_installations_gdf, self.rooftop_gdf, how="difference"
        )

        self.raw_PV_installations_off_rooftop["area_diff"] = (
            self.raw_PV_installations_off_rooftop["geometry"].to_crs(epsg=5243).area
        )

    def _ckdnearest(self, gdA, gdB):
        """
        Identifies the nearest points of GeoPandas.DataFrame gdB in GeoPandas.DataFrame gdA. Indices need to be resorted before using this function.

        Parameters
        ----------
        gdA : GeoPandas.GeoDataFrame
            GeoDataFrame which contains a column specifying shapely.geometry.Point objects for the centroids of the
            overhanging PV polygons
        gdB : GeoPandas.GeoDataFrame
            GeoDataFrame which contains a column specifying shapely.geometry.Point objects for the centroids of the
            intersected PV polygons
        Returns
        -------
        GeoPandas.GeoDataFrame
            Concatenated GeoPandas.GeoDataFrame containing all columns of both GeoDataFrames excluding gdB's
            geometry, i.e. the centroid of the intersected PV polygons, plus distance in degrees.
        """

        # List specifying the centroid coordinates of the overhanging PV polygons
        nA = np.array(list(zip(gdA.geometry.x, gdA.geometry.y)))

        # List specifying the centroid coordinates of the intersected PV polygons
        nB = np.array(list(zip(gdB.geometry.x, gdB.geometry.y)))

        btree = cKDTree(nB)

        # idx lists the index of the nearest neighbor in nB for each centroid in nA
        # dist specifies the respective distance between the nearest neighbors in degrees
        dist, idx = btree.query(nA, k=1)

        gdf = pd.concat(
            [
                gdA.reset_index(drop=True),
                gdB.loc[idx, gdB.columns != "geometry"].reset_index(drop=True),
                pd.Series(dist, name="dist_in_degrees"),
            ],
            axis=1,
        )

        # GeoDataFrame adding all the attributes of the nearest intersected PV polygon to the overhanging PV polygons
        return gpd.GeoDataFrame(gdf)

    def calculate_distance_in_meters_between_raw_overhanging_pv_installation_centroid_and_nearest_intersected_installation_centroid(
        self, nearest_address_gdf
    ):
        """
        Calculate the distance in meters between the centroid of the overhanging PV polygon, here points_no_data,
        and the PV polygon centroid which is intersected with a rooftop polygon, here address_points

        """

        # Centroid coordinates of intersected pv polygons
        address_points = list(
            zip(nearest_address_gdf["helper_x"], nearest_address_gdf["helper_y"])
        )

        # Centroid coordinates of overhanging pv polygons
        points_no_data = list(
            zip(nearest_address_gdf["geometry"].x, nearest_address_gdf["geometry"].y)
        )

        dist = [
            geopy.distance.geodesic(address, no_data).m
            for address, no_data in zip(address_points, points_no_data)
        ]

        nearest_address_gdf["dist_in_meters"] = pd.Series(dist)

        return nearest_address_gdf

    def identify_raw_overhanging_PV_installations(self):
        """
        Remove PV systems from raw_PV_installations_off_rooftop which are free-standing, i.e. only use the ones
        belonging to a rooftop.
        """

        # Free-standing units can be identified by the fact that their raw_area == area_diff
        self.raw_PV_installations_off_rooftop["checker"] = (
            self.raw_PV_installations_off_rooftop.raw_area
            - self.raw_PV_installations_off_rooftop.area_diff
        )

        self.raw_overhanging_PV_installations = self.raw_PV_installations_off_rooftop[
            self.raw_PV_installations_off_rooftop["checker"] > 0
        ]

        self.raw_overhanging_PV_installations = self.raw_overhanging_PV_installations[
            ["area_diff", "identifier", "geometry"]
        ]

        # Remove nan values which arise from corrupted rooftop geometries (rare)
        self.raw_overhanging_PV_installations = self.raw_overhanging_PV_installations[
            ~self.raw_overhanging_PV_installations.identifier.isnull()
        ]

        # Save the shape of the overhanging PV system polygon
        self.raw_overhanging_PV_installations[
            "geometry_overhanging_polygon"
        ] = self.raw_overhanging_PV_installations["geometry"]

        # Compute centroid of overhanging PV system polygons
        self.raw_overhanging_PV_installations[
            "geometry"
        ] = self.raw_overhanging_PV_installations["geometry"].centroid

    def filter_raw_overhanging_PV_installations(self):

        # Select all the PV polygon IDs which have been successfully intersected with a rooftop
        rooftop_pv_ids = (
            self.raw_PV_installations_on_rooftop.identifier.unique().tolist()
        )

        # Select all the overhanging PV polygons whose identifier matches with one of the intersected solar panels
        # mounted on a rooftop
        self.raw_overhanging_PV_installations = self.raw_overhanging_PV_installations[
            self.raw_overhanging_PV_installations.identifier.isin(rooftop_pv_ids)
        ]

        # Only consider cut-off geometries larger than 1 sqm
        self.raw_overhanging_PV_installations = self.raw_overhanging_PV_installations[
            self.raw_overhanging_PV_installations["area_diff"] > 1.0
        ]

    def enrich_raw_overhanging_pv_installations_with_closest_rooftop_attributes(self):

        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data = (
            self._ckdnearest(
                self.raw_overhanging_PV_installations,
                self.raw_PV_installations_on_rooftop,
            )
        )

        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
            "helper_x"
        ] = gpd.GeoSeries(
            raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
                "centroid_intersect"
            ]
        ).x
        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
            "helper_y"
        ] = gpd.GeoSeries(
            raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
                "centroid_intersect"
            ]
        ).y

        # Check if the identifier of the intersected polygon is the same as the identifier of the overhanging polygon
        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
            "checker"
        ] = (
            raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
                "identifier_diff"
            ]
            == raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
                "identifier"
            ]
        )

        # Calculate the distance in meters between the centroid of the overhanging PV polygon and the centroid of the
        # PV polygon which is intersected with a rooftop polygon
        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data = self.calculate_distance_in_meters_between_raw_overhanging_pv_installation_centroid_and_nearest_intersected_installation_centroid(
            raw_overhanging_pv_installations_enriched_with_closest_rooftop_data
        )

        # The value for the area of the intersected PV installation is updated by the area of the overhanging PV polygon
        # in order to aggregate the areas for a given rooftop later
        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
            "area_inter"
        ] = raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
            "area_diff"
        ]

        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data = (
            raw_overhanging_pv_installations_enriched_with_closest_rooftop_data[
                [
                    "raw_area",
                    "identifier",
                    "Area",
                    "Azimuth",
                    "Building_I",
                    "City",
                    "PostalCode",
                    "RoofTopID",
                    "RooftopTyp",
                    "Street",
                    "StreetNumb",
                    "Tilt",
                    "area_inter",
                    "geometry_overhanging_polygon",
                ]
            ]
        )

        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data.rename(
            columns={"geometry_overhanging_polygon": "geometry"}, inplace=True
        )

        return raw_overhanging_pv_installations_enriched_with_closest_rooftop_data

    def append_raw_overhanging_PV_installations_to_intersected_installations(self):

        # IMPORTANT: if ckdnearest is used always reset_index before
        self.raw_overhanging_PV_installations = (
            self.raw_overhanging_PV_installations.reset_index(drop=True)
        )

        self.raw_overhanging_PV_installations.rename(
            columns={"identifier": "identifier_diff"}, inplace=True
        )

        # Extract centroid from intersected PV polygons while preserving their polygon geometry
        self.raw_PV_installations_on_rooftop[
            "geometry_intersected_polygon"
        ] = self.raw_PV_installations_on_rooftop["geometry"]
        self.raw_PV_installations_on_rooftop[
            "geometry"
        ] = self.raw_PV_installations_on_rooftop["geometry"].centroid
        self.raw_PV_installations_on_rooftop[
            "centroid_intersect"
        ] = self.raw_PV_installations_on_rooftop["geometry"]

        raw_overhanging_pv_installations_enriched_with_closest_rooftop_data = (
            self.enrich_raw_overhanging_pv_installations_with_closest_rooftop_attributes()
        )

        self.raw_PV_installations_on_rooftop.geometry = (
            self.raw_PV_installations_on_rooftop.geometry_intersected_polygon
        )

        self.raw_PV_installations_on_rooftop = self.raw_PV_installations_on_rooftop[
            [
                "raw_area",
                "identifier",
                "Area",
                "Azimuth",
                "Building_I",
                "City",
                "PostalCode",
                "RoofTopID",
                "RooftopTyp",
                "Street",
                "StreetNumb",
                "Tilt",
                "area_inter",
                "geometry",
            ]
        ]

        # Append the dataframe of all raw overhanging PV installations, enriched with the
        # rooftop attributes of their nearest rooftop, to the dataframe of all intersected PV installations
        # Note 1: Attributes starting with capital letters specify rooftop attributes.
        # Note 2: The geometry of the overhanging PV installations is not yet dissolved with the geometry of the
        # intersected PV installations
        self.raw_PV_installations_on_rooftop = gpd.GeoDataFrame(
            self.raw_PV_installations_on_rooftop.append(
                raw_overhanging_pv_installations_enriched_with_closest_rooftop_data
            )
        ).reset_index(drop=True)

    def remove_erroneous_pv_polygons(self):

        # Compute share of raw area that the intersected pv polygon covers
        self.raw_PV_installations_on_rooftop["percentage_intersect"] = (
            self.raw_PV_installations_on_rooftop["area_inter"]
            / self.raw_PV_installations_on_rooftop["raw_area"]
        )

        # Group intersection by polygon identifier and sum percentage
        self.group_intersection_id = self.raw_PV_installations_on_rooftop.groupby(
            "identifier"
        ).agg(
            {
                "area_inter": "sum",
                "Street": "first",
                "Street_Address": "first",
                "raw_area": "first",
                "City": "first",
                "PostalCode": "first",
                "percentage_intersect": "sum",
            }
        )

        # Find erroneous polygons whose area after intersection is larger than their original (raw) area
        polygone = self.group_intersection_id[
            self.group_intersection_id["percentage_intersect"] > 1.1
        ].index.tolist()

        # Filter out erroneous polygons identified above and all their respective sub-parts
        self.raw_PV_installations_on_rooftop = (
            self.raw_PV_installations_on_rooftop.drop(
                self.raw_PV_installations_on_rooftop.index[
                    (self.raw_PV_installations_on_rooftop["identifier"].isin(polygone))
                    & (self.raw_PV_installations_on_rooftop["percentage_intersect"] < 1)
                ]
            )
        )

        # Drop duplicate identifiers for erroneous polygons
        self.raw_PV_installations_on_rooftop = (
            self.raw_PV_installations_on_rooftop.drop(
                self.raw_PV_installations_on_rooftop.index[
                    (self.raw_PV_installations_on_rooftop["identifier"].isin(polygone))
                    & (self.raw_PV_installations_on_rooftop["identifier"].duplicated())
                ]
            )
        )

    def correct_area_by_tilt(self):

        # Clip tilts to account for incorrect geometries
        # Two assumptions feed into these lines:
        # 1. Rooftop tilts larger than 50 degrees are unrealistic and likely due to erroneous data. We set them to a
        # standard tilt of 30 degrees
        # 2. PV panels are tilted in the same way as their underlying rooftop. On flat roofs, we assume a tilt angle
        # of 30 degrees
        self.raw_PV_installations_on_rooftop["Tilt"][
            self.raw_PV_installations_on_rooftop["Tilt"] >= 50
        ] = 30
        self.raw_PV_installations_on_rooftop["Tilt"][
            self.raw_PV_installations_on_rooftop["Tilt"] == 0
        ] = 30

        self.corrected_PV_installations_on_rooftop = (
            self.raw_PV_installations_on_rooftop
        )

        # Calculate corrected area by considering a rooftop's tilt
        self.corrected_PV_installations_on_rooftop["area_tilted"] = (
            1
            / self.corrected_PV_installations_on_rooftop["Tilt"]
            .apply(math.radians)
            .apply(math.cos)
        ) * self.corrected_PV_installations_on_rooftop["area_inter"]

    def _geocode_addresses(self, addresses):

        coordinates = []
        counter = 0

        for i in range(len(addresses)):

            counter += 1
            print(f"Geocode address {addresses[i]} at {counter}/{len(addresses)}")

            # Apply some sleep to ensure to be below 50 requests per second
            time.sleep(0.1)
            address = addresses[i]
            # g = geocoder.bing(address, key=self.bing_key)
            g = geocoder.osm(address)

            if g.status == "OK":

                coords = g.latlng
                coordinates.append(coords)

            else:

                print("status: {}".format(g.status))
                coordinates.append(",")

        return coordinates

    def create_registry_for_PV_installations(self):
        """
        Create an address-level PV registry by matching identified and segmented PV panels with their respective rooftop segments.

        Returns
        -------

        """

        self.aggregate_raw_PV_polygons_to_raw_PV_installations()

        self.overlay_raw_PV_installations_and_rooftops()

        self.identify_raw_overhanging_PV_installations()

        self.filter_raw_overhanging_PV_installations()

        self.append_raw_overhanging_PV_installations_to_intersected_installations()

        # Create street address column
        self.raw_PV_installations_on_rooftop["Street_Address"] = (
            self.raw_PV_installations_on_rooftop["Street"]
            + " "
            + self.raw_PV_installations_on_rooftop["StreetNumb"]
            + ", "
            + self.raw_PV_installations_on_rooftop["PostalCode"]
            + ", "
            + self.raw_PV_installations_on_rooftop["City"]
        )

        self.remove_erroneous_pv_polygons()

        self.correct_area_by_tilt()

        # Group by rooftop ID
        self.rooftop_registry = self.corrected_PV_installations_on_rooftop.dissolve(
            by="RoofTopID",
            aggfunc={
                "Azimuth": "first",
                "Tilt": "first",
                "area_inter": "sum",
                "area_tilted": "sum",
                "RoofTopID": "first",
                "Street": "first",
                "City": "first",
                "PostalCode": "first",
                "Street_Address": "first",
            },
        )

        # Group by street address
        self.address_registry = self.corrected_PV_installations_on_rooftop.dissolve(
            by="Street_Address",
            aggfunc={
                "area_inter": "sum",
                "area_tilted": "sum",
                "Street": "first",
                "City": "first",
                "PostalCode": "first",
                "Street_Address": "first",
            },
        )

        # Reset index for subsequent nearest neighbor search
        self.rooftop_registry.reset_index(drop=True, inplace=True)
        self.address_registry.reset_index(drop=True, inplace=True)

        """
        # You cannot save two columns with shapely objects to a geojson file
        addresses = (self.address_registry["Street_Address"]).tolist()

        coordinates = self._geocode_addresses(addresses)

        street_address_coords = gpd.GeoSeries(
            [
                Point(coord[1], coord[0])
                for coord in coordinates
                if isinstance(coord, list)
            ]
        )

        self.address_registry = pd.concat(
            [self.address_registry, street_address_coords], axis=1
        )

        self.address_registry = self.address_registry.rename(
            columns={0: "geocoded_street_address"}
        )
        
        """

        self.rooftop_registry["capacity_not_tilted"] = (
            self.rooftop_registry.area_inter / 6.5
        )

        self.rooftop_registry["capacity_tilted"] = (
            self.rooftop_registry.area_tilted / 6.5
        )

        self.address_registry["capacity_not_tilted"] = (
            self.address_registry.area_inter / 6.5
        )

        self.address_registry["capacity_tilted"] = (
            self.address_registry.area_tilted / 6.5
        )

        self.rooftop_registry.to_file(
            driver="GeoJSON",
            filename=f"data/pv_registry/{self.county}_rooftop_registry.geojson",
        )
        
        self.address_registry.to_file(
            driver="GeoJSON",
            filename=f"data/pv_registry/{self.county}_address_registry.geojson",
        )

if __name__ == "__main__":

    import yaml

    config_file = "/Users/kevin/Projects/Active/PV4GERFiles/pv4ger/config.yml"

    with open(config_file, "rb") as f:

        conf = yaml.load(f, Loader=yaml.FullLoader)

    RegistryCreator(conf).create_registry_for_PV_installations()
