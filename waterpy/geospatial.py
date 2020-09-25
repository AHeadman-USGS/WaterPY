from osgeo import ogr, gdal, osr, gdalconst
import os
import numpy as np
import pandas as pd
import json
import pycrs
import pyproj
import netCDF4
import datetime as dt


class Shp:
    """
    Contains various fuctions and metadata desc in init related in SHP objects.
    While titled SHP, currently this should only be used with polygons.  Will incorporate fun things like
    points in future versions.  I currently see no reason to incorporate lines.
    Outside reliance on the daymet.prj (included in ./static/geospatial) to transform things into daymet to build the
    temp/precip series.
    """

    def __init__(self, path):
        self.path = path
        self.shp = ogr.Open(self.path)
        self.lyr = self.shp.GetLayer()
        self.prj = self.shp.GetLayer().GetSpatialRef()
        self.prj4 = self.prj.ExportToProj4()
        self.feature = self.shp.GetLayer(0).GetFeature(0)
        self.extent = self.feature.GetGeometryRef().GetEnvelope()
        self.x_cen, self.y_cen = self._centroid()
        self.daymet_x, self.daymet_y = self.daymet_proj()
        self.karst_flag = 0

    @classmethod
    def _clean(cls, path):
        ds = ogr.Open(path, 1)
        lyr = ds.GetLayer()
        defn = lyr.GetLayerDefn()
        for i in range(defn.GetFieldCount()):
            name = defn.GetFieldDefn(i).GetName()
            if name == "Shape_Area" or name == "Shape_Leng":
                lyr.DeleteField(i)
            else:
                continue
        ds = None
        clean_shp = Shp(path=path)
        return clean_shp

    def _centroid(self):
        centroid = json.loads(
            self.feature.GetGeometryRef().Centroid().ExportToJson())
        center_x = centroid['coordinates'][0]
        center_y = centroid['coordinates'][1]
        return center_x, center_y

    def daymet_proj(self):
        daymet_proj = pycrs.load.from_file("shapefiles//Daymet.prj")
        transformer = pyproj.Transformer.from_crs(self.prj4, daymet_proj.to_proj4())
        return transformer.transform(self.x_cen, self.y_cen)


class dbShp:
    """
    Basically the same as Raster class, for shapes provided with DB or created by code.
    shps.
    """
    def __init__(self, path):
        self.path = path
        self.shp = ogr.Open(self.path)
        self.lyr = self.shp.GetLayer()
        self.prj = self.shp.GetLayer().GetSpatialRef()
        self.prj4 = self.prj.ExportToProj4()
        self.feature = self.shp.GetLayer(0).GetFeature(0)
        self.x_cen, self.y_cen = self._centroid()

    def _centroid(self):
        centroid = json.loads(
            self.feature.GetGeometryRef().Centroid().ExportToJson())
        center_x = centroid['coordinates'][0]
        center_y = centroid['coordinates'][1]
        return center_x, center_y

    def daymet_proj(self):
        daymet_proj = pycrs.load.from_file("shapefiles//Daymet.prj")
        transformer = pyproj.Transformer.from_crs(self.prj4, daymet_proj.to_proj4())
        return transformer.transform(self.x_cen, self.y_cen)


class Raster:
    """
       Contains various fuctions and metadata desc in init related in rasters objects.
       WaterPy internals (./static/geospatal/rasters) utilizes tifs, this object class is compatible with any
       osgeo/gdal compliant raster formats.
    """

    def __init__(self, path):
        self.path = path
        self.data = gdal.Open(self.path)
        self.band_1 = self.data.GetRasterBand(1)
        self.gt = self.data.GetGeoTransform
        self.prj = osr.SpatialReference(wkt=self.data.GetProjection())
        self.prj4 = self.prj.ExportToProj4()


def bbox_to_pixel_offsets(gt, bbox):
    """
    Function to offset (aka snap) polygon to raster.

    :param gt: geotransform variable from gdal.data.GetGeoTransform
    :param bbox: Bounding extent coordinates from ogr.feature.GetExtent()
    :return: tuple to use as a multiplier for the raster array.
    """

    origin_x = gt[0]
    origin_y = gt[3]
    pixel_width = gt[1]
    pixel_height = gt[5]
    x1 = int((bbox[0] - origin_x) / pixel_width)
    x2 = int((bbox[1] - origin_x) / pixel_width) + 1

    y1 = int((bbox[3] - origin_y) / pixel_height)
    y2 = int((bbox[2] - origin_y) / pixel_height) + 1

    xsize = x2 - x1
    ysize = y2 - y1
    return x1, y1, xsize, ysize


def karst_detection(raster, shp):
    """
    :param raster: Raster class object built from karst raster.
    :param shp: SHP class object from entire basin.
    :return: Shp.karst_flag will be triggered, or it won't.
    """

    r_data = raster.data
    r_band = r_data.GetRasterBand(1)
    r_geotransform = raster.gt()
    v_data = shp.shp
    v_feature = v_data.GetLayer(0)

    sourceprj = v_feature.GetSpatialRef()
    targetprj = osr.SpatialReference(wkt=r_data.GetProjection())

    if sourceprj.ExportToProj4() != targetprj.ExportToProj4():
        to_fill = ogr.GetDriverByName('Memory')
        ds = to_fill.CreateDataSource("project")
        outlayer = ds.CreateLayer('poly', targetprj, ogr.wkbPolygon)
        feature = v_feature.GetFeature(0)
        transform = osr.CoordinateTransformation(sourceprj, targetprj)
        transformed = feature.GetGeometryRef()
        transformed.Transform(transform)
        geom = ogr.CreateGeometryFromWkb(transformed.ExportToWkb())
        defn = outlayer.GetLayerDefn()
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        outlayer.CreateFeature(feat.Clone())
        feat = None
        v_feature = outlayer

    src_offset = bbox_to_pixel_offsets(r_geotransform, v_feature.GetExtent())
    src_array = r_band.ReadAsArray(*src_offset)

    new_gt = (
        (r_geotransform[0] + (src_offset[0] * r_geotransform[1])),
        r_geotransform[1], 0.0,
        (r_geotransform[3] + (src_offset[1] * r_geotransform[5])),
        0.0, r_geotransform[5]
    )

    stats = []  # Keeping this unless there are several features in the same shapefile.

    driver = gdal.GetDriverByName('MEM')
    v_to_r = driver.Create('', src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
    v_to_r.SetGeoTransform(new_gt)
    gdal.RasterizeLayer(v_to_r, [1], v_feature, burn_values=[1])
    v_to_r_array = v_to_r.ReadAsArray()
    masked = np.ma.MaskedArray(
        src_array,
        mask=np.logical_not(v_to_r_array)

    )

    if masked.max() > 0:
        return 1
    else:
        return 0


def zonal_stats(raster, shp):
    """
    Converts a shp file into a raster mask.  Masks off a polygon and extracts statistics from the area within the mask.
    Currently this only works with a shp file with one feature, however, it's written so that it could be adjusted to
    handle multiple features.

    :param raster: Raster class object.
    :param shp: Shp class object.
    :return: list of dict objects from computed stats.
    """

    r_data = raster.data
    r_band = r_data.GetRasterBand(1)
    nodata_value = r_band.GetNoDataValue()
    r_geotransform = raster.gt()
    v_data = shp.shp
    v_feature = v_data.GetLayer(0)

    sourceprj = v_feature.GetSpatialRef()
    targetprj = osr.SpatialReference(wkt=r_data.GetProjection())

    if sourceprj.ExportToProj4() != targetprj.ExportToProj4():
        to_fill = ogr.GetDriverByName('Memory')
        ds = to_fill.CreateDataSource("project")
        outlayer = ds.CreateLayer('poly', targetprj, ogr.wkbPolygon)
        feature = v_feature.GetFeature(0)
        transform = osr.CoordinateTransformation(sourceprj, targetprj)
        transformed = feature.GetGeometryRef()
        transformed.Transform(transform)
        geom = ogr.CreateGeometryFromWkb(transformed.ExportToWkb())
        defn = outlayer.GetLayerDefn()
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        outlayer.CreateFeature(feat.Clone())
        v_feature = outlayer

    src_offset = bbox_to_pixel_offsets(r_geotransform, v_feature.GetExtent())
    src_array = r_band.ReadAsArray(*src_offset)


    new_gt = (
        (r_geotransform[0] + (src_offset[0] * r_geotransform[1])),
        r_geotransform[1], 0.0,
        (r_geotransform[3] + (src_offset[1] * r_geotransform[5])),
        0.0, r_geotransform[5]
    )

    driver = gdal.GetDriverByName('MEM')
    stats = []

    v_to_r = driver.Create('', src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
    v_to_r.SetGeoTransform(new_gt)
    gdal.RasterizeLayer(v_to_r, [1], v_feature, burn_values=[1])
    v_to_r_array = v_to_r.ReadAsArray()
    src_array = np.array(src_array, dtype=float)
    v_to_r_array = np.array(v_to_r.ReadAsArray(), dtype=float)
    masked = np.ma.MaskedArray(
        src_array,
        mask=np.logical_or(
            src_array == nodata_value,
            np.logical_not(v_to_r_array)
        ),
        fill_value=np.nan
    )

    feature_stats = {
        'source': str(raster.path),
        'min': float(masked.min()),
        'mean': float(masked.mean()),
        'max': float(masked.max()),
        'std': float(masked.std()),
    }

    ds = None

    stats.append(feature_stats)
    return feature_stats['mean']


def zonal_area(raster, shp):
    """
    Converts a shp file into a raster mask.  Masks off a polygon and extracts statistics from the area within the mask.
    Currently this only works with a shp file with one feature, however, it's written so that it could be adjusted to
    handle multiple features.

    :param raster: Raster class object.
    :param shp: Shp class object.
    :return: list of dict objects from computed stats.
    """

    r_data = raster.data
    r_band = r_data.GetRasterBand(1)
    r_geotransform = raster.gt()
    v_data = shp.shp
    v_feature = v_data.GetLayer(0)
    nodata_value = r_band.GetNoDataValue()

    sourceprj = v_feature.GetSpatialRef()
    targetprj = osr.SpatialReference(wkt=r_data.GetProjection())

    if sourceprj.ExportToProj4() != targetprj.ExportToProj4():
        to_fill = ogr.GetDriverByName('Memory')
        ds = to_fill.CreateDataSource("project")
        outlayer = ds.CreateLayer('poly', targetprj, ogr.wkbPolygon)
        feature = v_feature.GetFeature(0)
        transform = osr.CoordinateTransformation(sourceprj, targetprj)
        transformed = feature.GetGeometryRef()
        transformed.Transform(transform)
        geom = ogr.CreateGeometryFromWkb(transformed.ExportToWkb())
        defn = outlayer.GetLayerDefn()
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        outlayer.CreateFeature(feat.Clone())
        feat = None
        v_feature = outlayer

    src_offset = bbox_to_pixel_offsets(r_geotransform, v_feature.GetExtent())
    src_array = r_band.ReadAsArray(*src_offset)

    new_gt = (
        (r_geotransform[0] + (src_offset[0] * r_geotransform[1])),
        r_geotransform[1], 0.0,
        (r_geotransform[3] + (src_offset[1] * r_geotransform[5])),
        0.0, r_geotransform[5]
    )

    driver = gdal.GetDriverByName('MEM')

    stats = []

    v_to_r = driver.Create('', src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
    v_to_r.SetGeoTransform(new_gt)
    gdal.RasterizeLayer(v_to_r, [1], v_feature, burn_values=[1])
    v_to_r_array = v_to_r.ReadAsArray()
    src_array = np.array(src_array, dtype=float)
    v_to_r_array = np.array(v_to_r.ReadAsArray(), dtype=float)
    masked = np.ma.MaskedArray(
        src_array,
        mask=np.logical_or(
            src_array == nodata_value,
            np.logical_not(v_to_r_array)
        ),
        fill_value=np.nan

    )

    return float(masked.count() * 100)


def twi_bins(raster, shp, nbins=30):

    r_data = raster.data
    r_band = r_data.GetRasterBand(1)
    r_geotransform = raster.gt()
    v_data = shp.shp
    v_feature = v_data.GetLayer(0)

    sourceprj = v_feature.GetSpatialRef()
    targetprj = osr.SpatialReference(wkt=r_data.GetProjection())

    if sourceprj.ExportToProj4() != targetprj.ExportToProj4():
        to_fill = ogr.GetDriverByName('Memory')
        ds = to_fill.CreateDataSource("project")
        outlayer = ds.CreateLayer('poly', targetprj, ogr.wkbPolygon)
        feature = v_feature.GetFeature(0)
        transform = osr.CoordinateTransformation(sourceprj, targetprj)
        transformed = feature.GetGeometryRef()
        transformed.Transform(transform)
        geom = ogr.CreateGeometryFromWkb(transformed.ExportToWkb())
        defn = outlayer.GetLayerDefn()
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        outlayer.CreateFeature(feat.Clone())
        feat = None
        v_feature = outlayer

    src_offset = bbox_to_pixel_offsets(r_geotransform, v_feature.GetExtent())
    src_array = r_band.ReadAsArray(*src_offset)

    new_gt = (
        (r_geotransform[0] + (src_offset[0] * r_geotransform[1])),
        r_geotransform[1], 0.0,
        (r_geotransform[3] + (src_offset[1] * r_geotransform[5])),
        0.0, r_geotransform[5]
    )

    driver = gdal.GetDriverByName('MEM')

    v_to_r = driver.Create('', src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
    v_to_r.SetGeoTransform(new_gt)
    gdal.RasterizeLayer(v_to_r, [1], v_feature, burn_values=[1])
    v_to_r_array = v_to_r.ReadAsArray()
    src_array = np.array(src_array, dtype=float)
    v_to_r_array = np.array(v_to_r.ReadAsArray(), dtype=float)
    masked = np.ma.MaskedArray(
        src_array,
        mask=np.logical_or(
            np.logical_not(v_to_r_array),
            src_array < 0)

    )

    mx = masked.max()
    mn = masked.min()
    mean = masked.mean()
    intvl = (mx - mn) / (nbins + 1)
    edges = np.arange(mn, mx, intvl)
    histo = np.histogram(masked, bins=edges)


    # need mean of each bin.  Get the rest of the stats while there.
    # TWI Mean is the value we need for TopModel Input.

    bins = []

    for i in range(nbins):
        line = []
        bin = i + 1
        if i == 0:
            twi_val = histo[1][i] / 2
        else:
            twi_val = (histo[1][i] + histo[1][i-1]) / 2
        proportion = histo[0][i]/np.sum(histo[0])

        line.append(bin)
        line.append(twi_val)
        line.append(proportion)
        bins.append(line)

    df = pd.DataFrame(bins, columns=['bin', 'twi', 'proportion'])
    df.set_index('bin', inplace=True)

    return df


def simplify(src):

    driver = ogr.GetDriverByName("ESRI Shapefile")
    src_ds = driver.Open(src.path, 0)
    src_layer = src_ds.GetLayer()
    out_path = src.path[:-4] + '_simple.shp'
    srs = osr.SpatialReference()
    srs.ImportFromProj4(src.prj4)
    if os.path.exists(out_path):
        driver.DeleteDataSource(out_path)

    out_ds = driver.CreateDataSource(out_path)
    out_layer = out_ds.CreateLayer('FINAL', srs=srs, geom_type=ogr.wkbPolygon)

    infeature = src_layer.GetFeature(0)
    outfeature = ogr.Feature(out_layer.GetLayerDefn())
    geom = infeature.geometry().Simplify(100.0)
    outfeature.SetGeometry(geom)
    out_layer.CreateFeature(outfeature)
    outfeature = None
    out_ds = None
    out_shp = dbShp(path=out_path)
    return out_shp


def clip(src, shp):
    """
    :param src: shapefile class with karst polygons.  sinks.shp in db.
    :param shp: shapefile class with basin boundary.
    :return: shapefile output and class with karst.
    """

    driver = ogr.GetDriverByName("ESRI Shapefile")
    src_ds = driver.Open(src.path, 0)
    src_layer = src_ds.GetLayer()
    clip_ds = driver.Open(shp.path, 0)
    clip_layer = clip_ds.GetLayer()
    clip_prj = clip_layer.GetSpatialRef()
    src_prj = src_layer.GetSpatialRef()


    if src.prj4 != shp.prj4:
        to_fill = ogr.GetDriverByName('Memory')
        ds = to_fill.CreateDataSource("project")
        out_layer = ds.CreateLayer('poly', src_prj, ogr.wkbPolygon)
        feature = shp.lyr.GetFeature(0)
        transform = osr.CoordinateTransformation(clip_prj, src_prj)
        transformed = feature.GetGeometryRef()
        transformed.Transform(transform)
        geom = ogr.CreateGeometryFromWkb(transformed.ExportToWkb())
        defn = out_layer.GetLayerDefn()
        feat = ogr.Feature(defn)
        feat.SetGeometry(geom)
        out_layer.CreateFeature(feat.Clone())
        clip_layer = out_layer

    srs = osr.SpatialReference()
    srs.ImportFromProj4(src.prj4)

    out_path = shp.path[:-4] + '_karst.shp'

    if os.path.exists(out_path):
        driver.DeleteDataSource(out_path)

    out_ds = driver.CreateDataSource(out_path)
    out_layer = out_ds.CreateLayer('FINAL', srs=srs, geom_type=ogr.wkbMultiPolygon)

    ogr.Layer.Clip(src_layer, clip_layer, out_layer)
    out_ds = None
    karstshp = dbShp(path=out_path)

    return karstshp


def erase(src, diff):
    #not working currently.

    driver = ogr.GetDriverByName("ESRI Shapefile")
    src_ds = driver.Open(src.path, 0)
    src_layer = src_ds.GetLayer()
    src_feature = src_layer.GetFeature(0)

    diff_ds = driver.Open(diff.path, 0)
    diff_layer = diff_ds.GetLayer()
    diff_feature = diff_layer.GetFeature(0)
    srs = osr.SpatialReference()
    srs.ImportFromProj4(src.prj4)

    out_path = shp.path[:-4] + '_notkarst.shp'
    if os.path.exists(out_path):
        driver.DeleteDataSource(out_path)

    out_ds = driver.CreateDataSource(out_path)
    out_layer = out_ds.CreateLayer('', srs=srs, geom_type=ogr.wkbMultiPolygon)
    out_defn = out_layer.GetLayerDefn()
    out_feature = ogr.Feature(out_defn)
    src_geom = src_feature.GetGeometryRef()
    diff_geom = diff_feature.GetGeometryRef()
    src_diff = src_geom.Difference(diff_geom)
    out_feature.SetGeometry(src_diff)

    wkt = out_feature.geometry().ExportToWkt()
    out_layer.CreateFeature(out_feature)
    karstless = dbShp(path=out_path)
    return karstless


def dissolve_polygon(raster, shp):
    """
    Needs work.

    :param raster: use karst_raster or any soil raster.  We just need the GT object
    :param shp: shp file to be dissolved
    :return: raster object of dissolved shp file.
    """
    gt = raster.gt()
    x_min = gt[0]
    y_max = gt[3]
    x_res = raster.data.RasterXSize
    y_res = raster.data.RasterYSize
    x_max = x_min + gt[1] * x_res
    y_min = y_max + gt[5] * y_res
    pixel_width = gt[1]
    out_file = "shapefiles//karst_flat.shp"
    target_ds = gdal.GetDriverByName('MEM').Create('', x_res, y_res, 1, gdal.GDT_Byte)
    target_ds.SetGeoTransform((x_min, pixel_width, 0, y_min, 0, pixel_width))
    band = target_ds.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.FlushCache()
    gdal.RasterizeLayer(target_ds, [1], shp.lyr, options=["ATTRIBUTE=Gridcode"])
    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(out_file)
    srs = osr.SpatialReference()
    srs.ImportFromProj4(shp.prj4)
    out_lyr = out_ds.CreateLayer(out_file, srs=srs)
    fd = ogr.FieldDefn("DN", ogr.OFTInteger)
    out_lyr.CreateField(fd)
    gdal.Polygonize(band, band, out_lyr, -1, [])
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in out_lyr:
        if feat.geometry():
            feat.geometry().CloseRings()  # this copies the first point to the end
            wkt = feat.geometry().ExportToWkt()
            multi.AddGeometryDirectly(ogr.CreateGeometryFromWkt(wkt))
            out_lyr.DeleteFeature(feat.GetFID())
    union = multi.UnionCascaded()
    out_feat = ogr.Feature(out_lyr.GetLayerDefn())
    out_feat.SetGeometry(union)
    out_lyr.CreateFeature(out_feat)

    flat = dbShp(path=out_file)
    target_ds = None
    return flat


def deg_lat(shp):
    in_srs = osr.SpatialReference()
    in_srs.ImportFromProj4(shp.prj4)
    out_srs = osr.SpatialReference()
    out_srs.ImportFromEPSG(4269)
    coord_transform = osr.CoordinateTransformation(in_srs, out_srs)
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint(shp.x_cen, shp.y_cen)
    point.Transform(coord_transform)

    return point.GetX()


def get_area(shp):
    geom = shp.feature.GetGeometryRef()
    area = geom.GetArea()
    return area


def characteristics(db_rasters, shp):
    characteristics_out = {
        "scaling_parameter" : zonal_stats(db_rasters['scaling_parameter'], shp) / 100,
        "saturated_hydraulic_conductivity" : zonal_stats(db_rasters['k_sat'], shp) / 100 * 86.4,
        "saturated_hydraulic_conductivity_multiplier" : zonal_stats(db_rasters['con_mult'], shp) / 100,
        "soil_depth_total" : zonal_stats(db_rasters["soil_thickness"], shp) / 10,
        "field_capacity_fraction" : zonal_stats(db_rasters["field_cap"], shp) / 10000,
        "porosity_fraction" : zonal_stats(db_rasters["porosity"], shp) / 10000,
        "wilting_point_fraction" : (zonal_stats(db_rasters["field_cap"], shp) / 10000) -
                                   (zonal_stats(db_rasters['awc'], shp) / 100),
        "latitude": deg_lat(shp),
        "basin_area_total": get_area(shp) / 10e6,
        "impervious_area_fraction": (zonal_area(db_rasters["imp"], shp) / get_area(shp)) * 100,
        "channel_length_max": 10,
        "channel_velocity_avg": 0.1,
        "flow_initial": 0.1,
        "stream area": zonal_area(db_rasters["imp"], shp) / 10e6,
        #lake_area: still working,
        "eff_imp" : 0.7,
        "imp_delay": 0.1,
        "twi_adj": 1,  #
        "et_exp_dorm" : 0,
        "et_exp_grow" : 0,
        "grow_trigger" : 15,
        "rip_area": (zonal_area(db_rasters["imp"], shp) / 10e6), # + lake_area,
        "lake_delay": 0
        #up_lake_area : still working
    }

    df = pd.DataFrame.from_dict(characteristics_out, orient="index")
    df.index.name = "name"
    df.columns = ['value']

    return df


def near(array, value):
    """
    array: 2d Array of values taken from daymet NetCDF input file.
    value: Input value derived from user provided shapefile.
    idx: Output for actual value nearest to user provided value.
    """

    # Helper function for build_prcp/build temps.  Finds the actual  nearest x, y coordinate in the matrix
    # to the user input coordinate.
    idx = (abs(array - value)).argmin()
    return idx


def build_prcp(f, x, y):
    """
    This needs a docstring!
    """

    # Read in and build the netCDF4 parameters
    nc = netCDF4.Dataset(f)
    lat = nc.variables['y'][:]
    lon = nc.variables['x'][:]
    time_var = nc.variables['time']
    dtime = netCDF4.num2date(time_var[:], time_var.units)

    # Building the indexes points.
    # By default this starts when Daymet starts, though this could be flexible.
    # Currently, this only accepts Daymet data.
    start = dt.datetime(1980, 1, 1)
    end = dt.datetime.utcnow()
    istart = netCDF4.date2index(start, time_var, select='nearest')
    istop = netCDF4.date2index(end, time_var, select='nearest')
    lati = y
    loni = x
    ix = near(lon, loni)
    iy = near(lat, lati)

    # Selecting the variables.
    prcp = nc.variables['prcp'][:]
    hs = prcp[0, istart:istop, ix, iy]
    tim = dtime[istart:istop]

    # Arranging data into pandas df.
    prcp_ts = pd.Series(hs, index=tim, name='precipitation (mm/day)')
    prcp_ts = pd.DataFrame(prcp_ts)
    prcp_ts.reset_index(inplace=True)
    prcp_ts.columns = ['Index', 'precipitation (mm/day)']
    prcp_ts['date'] = prcp_ts['Index']
    prcp_ts.set_index('Index', drop=True, inplace=True)

    return prcp_ts


def build_temps(f, x, y):
    """
    This also needs a docstring.
    """

    # Read in and build the netCDF4 parameters
    nc = netCDF4.Dataset(f)
    lat = nc.variables['y'][:]
    lon = nc.variables['x'][:]
    time_var = nc.variables['time']
    dtime = netCDF4.num2date(time_var[:], time_var.units)

    # Building the indexes points.
    # By default this starts when Daymet starts, though this could be flexible.
    # Currently, this only accepts Daymet data.
    start = dt.datetime(1980, 1, 1)
    end = dt.datetime.utcnow()
    istart = netCDF4.date2index(start, time_var, select='nearest')
    istop = netCDF4.date2index(end, time_var, select='nearest')
    lati = y
    loni = x
    ix = near(lon, loni)
    iy = near(lat, lati)

    # Selecting/subsetting the NetCDF dataset.
    temps = nc.variables['tmax'][:]
    hs = temps[0, istart:istop, ix, iy]
    tim = dtime[istart:istop]

    # Arranging data into pandas df.
    temps_ts = pd.Series(hs, index=tim, name='temperature (celsius)')
    temps_ts = pd.DataFrame(temps_ts)
    temps_ts.reset_index(inplace=True)
    temps_ts.columns = ['Index', 'temperature (celsius)']
    temps_ts['date'] = temps_ts['Index']
    temps_ts.set_index('Index', drop=True, inplace=True)

    return temps_ts




# Test code from here on down.

# shp = Shp(path="shapefiles//RockCastle.shp")
# grape = Shp(path="shapefiles//GrapeVine.shp")
#
# out_twi = twi_bins(db_rasters['twi'], shp)
# grape_df = characteristics(db_rasters, grape)
# grape_twi = twi_bins(db_rasters['twi'], grape)
#
# if shp.karst_flag == 1:
#     simple = simplify(shp)
#     karst = clip(karst_shp, simple)


if __name__ == "__main__":
    # Database header
    db_path = "database//"
    karst_raster = Raster(path="database//Sinks_masked.tif")
    db_rasters = {'awc': Raster(path=db_path + 'HA00_AWC.tif'),
                  'con_mult': Raster(path=db_path + 'HA00_cnmlt.tif'),
                  'field_cap': Raster(path=db_path + 'HA00_FC.tif'),
                  'k_sat': Raster(path=db_path + 'HA00_Ksat.tif'),
                  'scaling_parameter': Raster(path=db_path + 'HA00_m_1.tif'),
                  'soil_thickness': Raster(path=db_path + 'HA00_TH.tif'),
                  'porosity': Raster(path=db_path + 'HA00_POR.tif'),
                  'imp': Raster(path=db_path + 'IMP.tif'),
                  'twi': Raster(path=db_path + "TWI.tif"),
                  'stream': Raster(path=db_path + 'snet_10m.tif')
                  }
    karst_shp = Shp(path="database/karst_shp.shp")

    # Input goes here.
    print("path to shapefile:")
    path_to = input()
    print("Create time series? y/n")
    timeseries = input()
    if timeseries.capitalize() == "Y":
        timeseries = True
    else:
        timeseries = False
    shp = Shp(path=path_to)

    # Test code - don't delete.
    # shp = Shp(path="input//RockCastle.shp")
    # timeseries = False

    shp.karst_flag = karst_detection(karst_raster, shp)
    out_df = characteristics(db_rasters, shp)
    out_twi = twi_bins(db_rasters["twi"], shp)

    # Output
    out_df.to_csv("input//basin_characteristics.csv")
    out_twi.to_csv("input//twi.csv")
    if shp.karst_flag == 1:
        simple = simplify(shp)
        karst = clip(karst_shp, simple)
        out_df_karst = characteristics(db_rasters, karst)
        out_twi_karst = twi_bins(db_rasters["twi"], karst)
        out_df_karst.to_csv("input//basin_characteristics_karst.csv")
        out_twi_karst.to_csv("input//twi_karst.csv")

    if timeseries:
        for root, dirs, files in os.walk("database//climate", topdown=False):
            for file in files:
                file = os.path.join(root, file)
                if file.endswith("x.nc"):
                    if not "temp_ts" in globals():
                        temp_ts = build_temps(file, shp.x_cen, shp.y_cen)
                    else:
                        temp_append = build_temps(file, shp.x_cen, shp.y_cen)
                        temp_ts = temp_ts.append(temp_append)
                elif file.endswith("p.nc"):
                    if not "prcp_ts" in globals():
                        prcp_ts = build_prcp(file, shp.x_cen, shp.y_cen)
                    else:
                        prcp_append = build_prcp(file, shp.x_cen, shp.y_cen)
                        prcp_ts = prcp_ts.append(prcp_append)

        climate_ts = pd.merge(prcp_ts, temp_ts, on="date")
        col = climate_ts.columns.tolist()
        col.append(col.pop(0))
        climate_ts = climate_ts[col]
        climate_ts.to_csv("input//timeseries.csv")


